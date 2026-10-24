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


def _public_address(host: str, port: int, resolve: Resolver) -> str:
    """An address of `host` to connect to, when every address it has is a
    public one; a private, loopback or link-local one refuses the hop."""
    try:
        addresses = list(resolve(host, port))
    except OSError:
        addresses = []
    if not addresses:
        raise CheckFailure(f"fetch: {host} does not resolve")
    for raw in addresses:
        address = ipaddress.ip_address(raw)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        if not address.is_global or address.is_multicast:
            raise CheckFailure(f"fetch: {host} resolves to {raw}, not a public address")
    return addresses[0]


def _next_hop(url: str, location: str, allowed_hosts: Iterable[str]) -> str:
    target = urljoin(url, location)
    try:
        return validate_target(target, allowed_hosts)
    except InvalidTarget as exc:
        raise CheckFailure(f"redirect: to {ascii(target)[:200]}: {exc}") from None


def _read_capped(response: httpx.Response, deadline: float) -> bytes:
    body = bytearray()
    for chunk in response.iter_bytes():
        body += chunk
        if len(body) >= MAX_BODY_BYTES:
            break
        if time.monotonic() > deadline:
            raise CheckFailure("fetch: the page took too long to load")
    return bytes(body[:MAX_BODY_BYTES])


def fetch(
    url: str,
    allowed_hosts: Iterable[str],
    *,
    resolve: Resolver = _resolve,
    transport: httpx.BaseTransport | None = None,
) -> Page:
    """GET `url`, following at most MAX_REDIRECTS redirects, each checked as
    a target is. Blocking: run it in a worker thread.

    Each request goes to the address that was checked, with the host name
    in Host and SNI, so a second DNS answer cannot move it elsewhere.
    """
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
            parts = urlsplit(url)
            host = parts.hostname or ""
            port = parts.port or 443
            address = _public_address(host, port, resolve)
            netloc = f"[{address}]" if ":" in address else address
            try:
                with client.stream(
                    "GET",
                    urlunsplit(("https", f"{netloc}:{port}", parts.path or "/", parts.query, "")),
                    headers={"Host": parts.netloc},
                    extensions={"sni_hostname": host},
                ) as response:
                    if response.has_redirect_location:
                        url = _next_hop(url, response.headers["Location"], allowed_hosts)
                        continue
                    body = _read_capped(response, deadline)
                    return Page(
                        status=response.status_code,
                        final_url=url,
                        body=body.decode(response.encoding or "utf-8", errors="replace"),
                    )
            except httpx.HTTPError as exc:
                raise CheckFailure(f"fetch: {type(exc).__name__}: {exc}"[:300]) from exc
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
    """Export `link`'s state; an unchecked or disabled one exports nothing."""
    if not link.enabled or link.last_check_at is None:
        metrics.forget_link(link.name)
        return
    metrics.publish_link(
        link.name,
        ok=link.alert_state != ALERT_FAILING,
        checked_at=link.last_check_at.timestamp(),
        failure=link.last_check_detail,
    )


async def check_link(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings, name: str
) -> CheckResult | None:
    """Check `name` now and record the result. None when the link was
    removed or edited during the check, which then records nothing."""
    async with session_factory() as session:
        link = await session.get(Link, name)
        if link is None:
            return None
        target_url, expectations = link.target_url, list(link.expectations)
    # No transaction is open across the fetch.
    result = await run_check(target_url, expectations, settings.links_allowed_hosts)
    async with session_scope(session_factory) as session:
        link = await session.get(Link, name, with_for_update=True)
        if link is None or (link.target_url, link.expectations) != (target_url, expectations):
            return None
        link.last_check_at = datetime.now(UTC)
        link.last_check_ok = result.ok
        link.last_check_detail = result.detail
        link.alert_state, change = next_alert_state(link.alert_state, result.ok)
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
