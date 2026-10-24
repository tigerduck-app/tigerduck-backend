"""Checks each enabled link's target and keeps the result on its row.

A check fetches the target the way the apps do, follows its redirects
itself and runs the link's rules on the page. The row's `alert_state`
flips on the first failed check and on the first passing one after it;
`server.metrics` exports that state, and Grafana alerts on it
(`monitoring/grafana/provisioning/alerting/rules.yml`).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server import metrics
from server.admin_audit import AdminAuditLog
from server.config import Settings
from server.db import session_scope
from server.links.models import ALERT_FAILING, ALERT_OK, Link
from server.links.rules import Page, first_failure
from server.links.targets import InvalidTarget, validate_target

logger = structlog.get_logger(__name__)

# The iPhone app's NTUSTSessionManager.browserUserAgent
# (swift/TigerDuck/Services/API/NTUST/NTUSTSessionManager.swift in
# tigerduck-app) on iOS 26.5. NTUST answers 403 to a request without one.
USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 26_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 "
    "Mobile/15E148 Safari/604.1"
)

MAX_REDIRECTS = 5
MAX_BODY_BYTES = 2 * 1024 * 1024
# The whole check, every hop included; each request also has its own limit.
CHECK_TIMEOUT_SECONDS = 30.0
REQUEST_TIMEOUT_SECONDS = 10.0

Resolver = Callable[[str, int], Sequence[str]]


class CheckFailure(Exception):
    """A check that failed before its rules ran: `<rule>: <what was found>`."""


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    detail: str


def _resolve(host: str, port: int) -> list[str]:
    return [
        info[4][0]
        for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    ]


# ipaddress counts it global, but a NAT64 gateway maps it onto any IPv4
# address, private ones included.
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _public_addresses(host: str, resolve: Resolver) -> list[str]:
    """The addresses of `host` to try, when every address it has is a public
    one; a private, loopback or link-local one refuses the hop."""
    try:
        addresses = list(dict.fromkeys(resolve(host, 443)))
    except OSError:
        addresses = []
    if not addresses:
        raise CheckFailure(f"fetch: {host} does not resolve")
    for raw in addresses:
        address = ipaddress.ip_address(raw)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        if not address.is_global or address.is_multicast or address in _NAT64:
            raise CheckFailure(f"fetch: {host} resolves to {raw}, not a public address")
    return addresses


def _next_hop(url: str, location: str, allowed_hosts: Iterable[str]) -> str:
    try:
        target = urljoin(url, location)
    except ValueError:
        target = location
    try:
        return validate_target(target, allowed_hosts)
    except InvalidTarget as exc:
        raise CheckFailure(f"redirect: to {ascii(target)[:200]}: {exc}") from None


def _read_capped(response: httpx.Response, deadline: float) -> bytes:
    # Raw bytes: a compressed body could inflate far past the cap in one chunk.
    encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
    if encoding not in ("", "identity"):
        raise CheckFailure(f"fetch: the page is sent with Content-Encoding {encoding}")
    body = bytearray()
    for chunk in response.iter_raw():
        body += chunk
        if len(body) >= MAX_BODY_BYTES:
            break
        if time.monotonic() > deadline:
            raise CheckFailure("fetch: the page took too long to load")
    return bytes(body[:MAX_BODY_BYTES])


def _request(
    client: httpx.Client, url: str, address: str, deadline: float
) -> Page | str:
    """The page at `url`, asked of `address`, or the Location it redirects to."""
    if time.monotonic() > deadline:
        raise CheckFailure("fetch: the page took too long to load")
    parts = urlsplit(url)
    netloc = f"[{address}]" if ":" in address else address
    with client.stream(
        "GET",
        urlunsplit(("https", netloc, parts.path or "/", parts.query, "")),
        headers={"Host": parts.netloc},
        extensions={"sni_hostname": parts.hostname},
    ) as response:
        if response.has_redirect_location:
            return response.headers["Location"]
        body = _read_capped(response, deadline)
        return Page(
            status=response.status_code,
            final_url=url,
            body=body.decode(response.encoding or "utf-8", errors="replace"),
        )


def _describe(exc: httpx.HTTPError) -> str:
    return f"fetch: {type(exc).__name__}: {exc}"[:300]


def fetch(
    url: str,
    allowed_hosts: Iterable[str],
    *,
    resolve: Resolver = _resolve,
    transport: httpx.BaseTransport | None = None,
) -> Page:
    """GET `url`, checked as a target is, following at most MAX_REDIRECTS
    redirects, each checked too. Blocking: run it in a worker thread.

    Each request goes to an address that was checked, with the host name in
    Host and SNI, so a second DNS answer cannot move it elsewhere. An address
    that cannot be reached hands over to the next.
    """
    try:
        validate_target(url, allowed_hosts)
    except InvalidTarget as exc:
        raise CheckFailure(f"invalid target: {exc}") from None
    deadline = time.monotonic() + CHECK_TIMEOUT_SECONDS
    with httpx.Client(
        transport=transport,
        timeout=REQUEST_TIMEOUT_SECONDS,
        # Each hop is verified for its own host name, so no connection is reused.
        limits=httpx.Limits(max_keepalive_connections=0),
        trust_env=False,
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            unreachable: httpx.HTTPError | None = None
            for address in _public_addresses(urlsplit(url).hostname or "", resolve):
                try:
                    answer = _request(client, url, address, deadline)
                    break
                except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                    unreachable = exc
                except httpx.HTTPError as exc:
                    raise CheckFailure(_describe(exc)) from exc
            else:
                raise CheckFailure(_describe(unreachable)) from unreachable
            if isinstance(answer, Page):
                return answer
            url = _next_hop(url, answer, allowed_hosts)
        raise CheckFailure(f"redirect: more than {MAX_REDIRECTS} redirects")


async def run_check(
    target_url: str, expectations: list[dict], allowed_hosts: Iterable[str]
) -> CheckResult:
    try:
        page = await asyncio.wait_for(
            asyncio.to_thread(fetch, target_url, list(allowed_hosts)),
            timeout=CHECK_TIMEOUT_SECONDS + 5,
        )
    except TimeoutError:
        return CheckResult(False, "fetch: no answer in time")
    except CheckFailure as exc:
        return CheckResult(False, str(exc))
    failure = first_failure(page, expectations)
    if failure is not None:
        return CheckResult(False, failure)
    return CheckResult(True, f"{page.status} from {urlsplit(page.final_url).hostname}")


def next_alert_state(current: str, ok: bool) -> tuple[str, str | None]:
    """The alert state after a check and the change, if any: "failing" or
    "recovered". A link that keeps failing changes nothing."""
    if not ok and current != ALERT_FAILING:
        return ALERT_FAILING, "failing"
    if ok and current == ALERT_FAILING:
        return ALERT_OK, "recovered"
    return current, None


def publish(link: Link) -> None:
    """Export `link`'s state. A disabled link, and one never checked that
    has nothing to alert on, export no state."""
    failing = link.alert_state == ALERT_FAILING
    if not link.enabled or (link.last_check_at is None and not failing):
        metrics.forget_link(link.name)
        return
    metrics.publish_link(
        link.name,
        ok=not failing,
        checked_at=link.last_check_at.timestamp() if link.last_check_at else None,
        failure=link.alert_detail,
    )


async def check_link(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    name: str,
    *,
    actor: str | None = None,
) -> CheckResult | None:
    """Check `name` now and record the result, unless a check that started
    later recorded its own first. None when the link was removed or edited
    during the check, which then records nothing. An operator's check
    (`actor`) writes its `link.test` audit row with the result."""
    async with session_factory() as session:
        link = await session.get(Link, name)
        if link is None:
            return None
        target_url, expectations = link.target_url, list(link.expectations)
    started = datetime.now(UTC)
    # No transaction is open across the fetch.
    result = await run_check(target_url, expectations, settings.links_allowed_hosts)
    async with session_scope(session_factory) as session:
        link = await session.get(Link, name, with_for_update=True)
        if link is None or (link.target_url, link.expectations) != (target_url, expectations):
            return None
        if actor is not None:
            session.add(
                AdminAuditLog(
                    actor=actor, action="link.test", detail={"name": name, "ok": result.ok}
                )
            )
        if link.last_check_at is not None and link.last_check_at > started:
            return result
        link.last_check_at = started
        link.last_check_ok = result.ok
        link.last_check_detail = result.detail
        link.alert_state, change = next_alert_state(link.alert_state, result.ok)
        if change == "failing":
            link.alert_detail = result.detail
        elif change == "recovered":
            link.alert_detail = None
    if change == "failing":
        logger.warning("links.check_failing", name=name, detail=result.detail)
    elif change == "recovered":
        logger.info("links.check_recovered", name=name, detail=result.detail)
    publish(link)
    return result


async def run_link_checks(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> int:
    """The scheduler job: export every link's state from its row, then check
    the enabled links that are due. Runs at startup too, so the alert state
    is back before Grafana next evaluates."""
    async with session_factory() as session:
        links = (await session.execute(select(Link))).scalars().all()
    for link in links:
        publish(link)
    for name in metrics.published_links() - {link.name for link in links}:
        metrics.forget_link(name)
    now = datetime.now(UTC)
    due = [
        link.name
        for link in links
        if link.enabled
        and (
            link.last_check_at is None
            or link.last_check_at + timedelta(minutes=link.check_interval_minutes) <= now
        )
    ]
    for name in due:
        await check_link(session_factory, settings, name)
    return len(due)
