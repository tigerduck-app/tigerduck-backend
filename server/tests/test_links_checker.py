"""The link checker: how it fetches a target, the rules it runs on the page,
and the alert state it keeps on the row and exports. DNS and HTTP are
stand-ins; no test reaches a real host."""

from __future__ import annotations

import socket
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from prometheus_client import REGISTRY
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from server import metrics
from server.db import build_session_factory
from server.links import checker
from server.links.checker import (
    MAX_BODY_BYTES,
    USER_AGENT,
    CheckFailure,
    fetch,
    next_alert_state,
)
from server.links.models import ALERT_FAILING, ALERT_OK, Link
from server.links.rules import Page, Rules, first_failure

ALLOWED = ["ntust.edu.tw"]
CALENDAR = "https://www.academic.ntust.edu.tw/p/404-1048-78935.php"
CALENDAR_RULES = [
    {"type": "status", "equals": 200},
    {"type": "final_host", "in": ["www.academic.ntust.edu.tw"]},
    {"type": "href_suffix_count", "suffix": ".ics", "min": 1},
]
CALENDAR_PAGE = (
    '<html><body><a href="/var/file/114.ics">114 學年度行事曆</a>'
    '<a href="/var/file/notes.pdf">notes</a></body></html>'
)
DNS = {
    "www.academic.ntust.edu.tw": ["140.118.31.10"],
    "www.ntust.edu.tw": ["140.118.31.20", "2001:288:6008::20"],
}


class Resolver:
    def __init__(self, table: dict[str, list[str]]) -> None:
        self.table = table
        self.asked: list[str] = []

    def __call__(self, host: str, port: int) -> list[str]:
        self.asked.append(host)
        if host not in self.table:
            raise socket.gaierror("no such host")
        return self.table[host]


def _streamed(response: httpx.Response) -> httpx.Response:
    """`response` with its body unread, as one off the network arrives; the
    checker reads raw bytes, which a response built with content has spent."""
    return httpx.Response(
        response.status_code,
        headers=response.headers,
        stream=httpx.ByteStream(response.content),
    )


class Server:
    """Answers by (Host header, path); records every request it got."""

    def __init__(self, pages: dict[tuple[str, str], httpx.Response]) -> None:
        self.pages = pages
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return _streamed(self.pages[(request.headers["host"], request.url.path)])


def _redirect(location: str, status: int = 302) -> httpx.Response:
    return httpx.Response(status, headers={"Location": location})


def _fetch(url: str, server: Server, dns: dict[str, list[str]] = DNS) -> Page:
    return fetch(url, ALLOWED, resolve=Resolver(dns), transport=server.transport)


# --- fetch ------------------------------------------------------------------


def test_the_page_comes_from_the_checked_address_named_by_host_and_sni() -> None:
    server = Server(
        {("www.academic.ntust.edu.tw", "/p/404-1048-78935.php"): httpx.Response(200, text="ok")}
    )

    page = _fetch(CALENDAR, server)

    assert page == Page(status=200, final_url=CALENDAR, body="ok")
    [request] = server.requests
    assert request.url.host == "140.118.31.10"
    assert request.headers["Host"] == "www.academic.ntust.edu.tw"
    assert request.extensions["sni_hostname"] == "www.academic.ntust.edu.tw"
    assert request.headers["User-Agent"] == USER_AGENT


def test_redirects_are_followed_and_the_last_hop_is_the_final_url() -> None:
    server = Server(
        {
            ("www.ntust.edu.tw", "/calendar"): _redirect("/moved", 301),
            ("www.ntust.edu.tw", "/moved"): _redirect(CALENDAR, 307),
            ("www.academic.ntust.edu.tw", "/p/404-1048-78935.php"): httpx.Response(200),
        }
    )

    page = _fetch("https://www.ntust.edu.tw/calendar", server)

    assert page.final_url == CALENDAR
    assert [r.url.host for r in server.requests] == [
        "140.118.31.20",
        "140.118.31.20",
        "140.118.31.10",
    ]


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.8",
        "172.16.4.4",
        "192.168.1.1",
        "127.0.0.1",
        "169.254.169.254",
        "::1",
        "fe80::1",
        "fd00::1",
        "::ffff:127.0.0.1",
    ],
)
def test_a_hop_whose_host_resolves_to_a_private_address_is_refused(address: str) -> None:
    server = Server({("www.ntust.edu.tw", "/"): _redirect("https://inner.ntust.edu.tw/")})
    dns = {**DNS, "inner.ntust.edu.tw": [address]}

    with pytest.raises(CheckFailure, match="not a public address"):
        _fetch("https://www.ntust.edu.tw/", server, dns)

    # Refused before anything was sent to it.
    assert [r.headers["host"] for r in server.requests] == ["www.ntust.edu.tw"]


def test_a_nat64_address_is_refused() -> None:
    dns = {"www.ntust.edu.tw": ["64:ff9b::a00:8"]}

    with pytest.raises(CheckFailure, match="not a public address"):
        _fetch("https://www.ntust.edu.tw/", Server({}), dns)


def _unreachable_first(*reachable: str):
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.host not in reachable:
            raise httpx.ConnectError("connection refused", request=request)
        return _streamed(httpx.Response(200, text="ok"))

    return httpx.MockTransport(handle), seen


def test_an_unreachable_address_hands_over_to_the_next() -> None:
    transport, seen = _unreachable_first("2001:288:6008::20")

    page = fetch(
        "https://www.ntust.edu.tw/", ALLOWED, resolve=Resolver(DNS), transport=transport
    )

    assert page.body == "ok"
    assert seen == ["140.118.31.20", "2001:288:6008::20"]


def test_a_host_none_of_whose_addresses_answers_fails_the_check() -> None:
    transport, seen = _unreachable_first()

    with pytest.raises(CheckFailure, match="fetch: ConnectError"):
        fetch(
            "https://www.ntust.edu.tw/", ALLOWED, resolve=Resolver(DNS), transport=transport
        )
    assert seen == ["140.118.31.20", "2001:288:6008::20"]


def test_a_compressed_page_is_refused_unread() -> None:
    server = Server(
        {
            ("www.ntust.edu.tw", "/"): httpx.Response(
                200, headers={"Content-Encoding": "gzip"}, content=b"\x1f\x8b"
            )
        }
    )

    with pytest.raises(CheckFailure, match="Content-Encoding gzip"):
        _fetch("https://www.ntust.edu.tw/", server)
    assert server.requests[0].headers["Accept-Encoding"] == "identity"


def test_one_private_address_among_public_ones_refuses_the_first_hop_too() -> None:
    server = Server({})
    dns = {"www.ntust.edu.tw": ["140.118.31.20", "10.0.0.8"]}

    with pytest.raises(CheckFailure, match="10.0.0.8"):
        _fetch("https://www.ntust.edu.tw/", server, dns)
    assert server.requests == []


@pytest.mark.parametrize(
    ("location", "reason"),
    [
        ("https://evil.example/", "not an allowed host"),
        ("http://www.ntust.edu.tw/", "https"),
        ("https://user@www.ntust.edu.tw/", "user name"),
    ],
)
def test_a_redirect_a_target_could_not_have_is_refused_unresolved(
    location: str, reason: str
) -> None:
    server = Server({("www.ntust.edu.tw", "/"): _redirect(location)})
    resolver = Resolver(DNS)

    with pytest.raises(CheckFailure, match=reason):
        fetch("https://www.ntust.edu.tw/", ALLOWED, resolve=resolver, transport=server.transport)
    assert resolver.asked == ["www.ntust.edu.tw"]


def test_a_target_the_allowed_hosts_no_longer_cover_is_not_fetched() -> None:
    server = Server({})
    resolver = Resolver(DNS)

    with pytest.raises(
        CheckFailure,
        match="invalid target: the host www.academic.ntust.edu.tw is not an allowed host",
    ):
        fetch(CALENDAR, ["library.example"], resolve=resolver, transport=server.transport)
    assert resolver.asked == []
    assert server.requests == []


def test_a_redirect_to_a_malformed_url_fails_the_check() -> None:
    server = Server({("www.ntust.edu.tw", "/"): _redirect("https://[broken/")})

    with pytest.raises(
        CheckFailure, match=r"redirect: to 'https://\[broken/': the URL cannot be parsed"
    ):
        _fetch("https://www.ntust.edu.tw/", server)


def test_five_redirects_are_followed_and_a_sixth_is_not() -> None:
    hops = {
        ("www.ntust.edu.tw", f"/{i}"): _redirect(f"/{i + 1}") for i in range(6)
    }
    server = Server({**hops, ("www.ntust.edu.tw", "/5"): httpx.Response(200)})
    assert _fetch("https://www.ntust.edu.tw/0", server).status == 200

    server = Server({**hops, ("www.ntust.edu.tw", "/6"): httpx.Response(200)})
    with pytest.raises(CheckFailure, match="more than 5 redirects"):
        _fetch("https://www.ntust.edu.tw/0", server)
    assert len(server.requests) == 6


def test_the_body_is_read_up_to_2_mb() -> None:
    body = "a" * MAX_BODY_BYTES + "行事曆"
    server = Server({("www.ntust.edu.tw", "/"): httpx.Response(200, text=body)})

    page = _fetch("https://www.ntust.edu.tw/", server)

    assert len(page.body) == MAX_BODY_BYTES
    assert first_failure(page, [{"type": "contains", "text": "行事曆"}]) is not None


def test_a_host_that_does_not_resolve_fails_the_check() -> None:
    with pytest.raises(CheckFailure, match="does not resolve"):
        _fetch("https://gone.ntust.edu.tw/", Server({}))


def test_a_transport_error_fails_the_check() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(CheckFailure, match="fetch: ConnectError"):
        fetch(
            "https://www.ntust.edu.tw/",
            ALLOWED,
            resolve=Resolver(DNS),
            transport=httpx.MockTransport(refuse),
        )


# --- rules ------------------------------------------------------------------


def _page(status: int = 200, url: str = CALENDAR, body: str = CALENDAR_PAGE) -> Page:
    return Page(status=status, final_url=url, body=body)


def test_the_calendar_page_passes_the_calendar_rules() -> None:
    assert first_failure(_page(), CALENDAR_RULES) is None


@pytest.mark.parametrize(
    ("rule", "page", "failure"),
    [
        (
            {"type": "status", "equals": 200},
            _page(status=503),
            "status: expected 200, found 503",
        ),
        (
            {"type": "final_host", "in": ["www.academic.ntust.edu.tw"]},
            _page(url="https://www.ntust.edu.tw/404"),
            "final_host: expected www.academic.ntust.edu.tw, found www.ntust.edu.tw",
        ),
        (
            {"type": "contains", "text": "行事曆"},
            _page(body="<p>維護中</p>"),
            "contains: '行事曆' not found",
        ),
        (
            {"type": "href_suffix_count", "suffix": ".ics", "min": 2},
            _page(),
            "href_suffix_count: expected at least 2 links ending .ics, found 1",
        ),
    ],
)
def test_each_rule_type_reports_what_it_found(rule: dict, page: Page, failure: str) -> None:
    assert first_failure(page, [rule]) == failure


@pytest.mark.parametrize(
    "rule",
    [
        {"type": "status", "equals": 200},
        {"type": "final_host", "in": ["WWW.academic.ntust.edu.tw"]},
        {"type": "contains", "text": "行事曆"},
        {"type": "href_suffix_count", "suffix": ".ICS", "min": 1},
    ],
)
def test_each_rule_type_passes_a_page_that_meets_it(rule: dict) -> None:
    assert first_failure(_page(), [rule]) is None


def test_every_rule_must_pass_and_the_first_failure_is_reported() -> None:
    rules = [*CALENDAR_RULES, {"type": "contains", "text": "missing"}]
    assert first_failure(_page(status=500), rules) == "status: expected 200, found 500"
    assert first_failure(_page(), rules) == "contains: 'missing' not found"


@pytest.mark.parametrize(
    "rule",
    [
        {"type": "regex", "pattern": ".*"},
        {"type": "contains", "text": "x", "regex": True},
        {"type": "status", "equals": 99},
        {"type": "final_host", "in": []},
        {"type": "href_suffix_count", "suffix": ".ics", "min": 0},
    ],
)
def test_only_the_fixed_rule_types_are_accepted(rule: dict) -> None:
    with pytest.raises(ValueError):
        Rules.validate_python([rule])
    assert first_failure(_page(), [rule]) == (
        "expectations: the stored rules are not valid"
    )


# --- alert state ------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "ok", "expected"),
    [
        (ALERT_OK, True, (ALERT_OK, None)),
        (ALERT_OK, False, (ALERT_FAILING, "failing")),
        (ALERT_FAILING, False, (ALERT_FAILING, None)),
        (ALERT_FAILING, True, (ALERT_OK, "recovered")),
    ],
)
def test_the_alert_state_changes_only_on_a_transition(current, ok, expected) -> None:
    assert next_alert_state(current, ok) == expected


def _sample(metric: str, **labels: str) -> float | None:
    return REGISTRY.get_sample_value(metric, labels)


def _failing_series(name: str) -> dict[str, float]:
    return {
        sample.labels["failure"]: sample.value
        for family in REGISTRY.collect()
        if family.name == "tigerduck_link_check_failing"
        for sample in family.samples
        if sample.labels["name"] == name
    }


class Pages:
    """Stands in for `checker.fetch`: answers with the next page in turn."""

    def __init__(self, *pages: Page | CheckFailure) -> None:
        self.pages = list(pages)
        self.urls: list[str] = []

    def __call__(self, url: str, allowed_hosts) -> Page:
        self.urls.append(url)
        page = self.pages.pop(0)
        if isinstance(page, CheckFailure):
            raise page
        return page


@pytest.fixture
def factory(db_session: AsyncSession, prepared_engine: AsyncEngine):
    return build_session_factory(prepared_engine)


@pytest.fixture
def link_name(request):
    # The metrics are process-wide; a name per test keeps their series apart.
    name = "t-" + request.node.name.removeprefix("test_")[:50].replace("_", "-")
    yield name
    _new_process(name)


def _new_process(name: str) -> None:
    """What a restart leaves of `name`'s series: nothing."""
    metrics.forget_link(name)
    metrics.LINK_CHECK_FAILING.remove_by_labels({"name": name})
    metrics._link_failures.pop(name, None)


async def _add_link(factory, name: str, **fields) -> None:
    async with factory() as session:
        session.add(
            Link(
                name=name,
                target_url=CALENDAR,
                expectations=CALENDAR_RULES,
                updated_by="test",
                **fields,
            )
        )
        await session.commit()


async def _row(factory, name: str) -> Link:
    async with factory() as session:
        return await session.get(Link, name)


db_test = pytest.mark.asyncio(loop_scope="session")


@db_test
async def test_a_link_alerts_once_as_it_fails_and_once_as_it_recovers(
    factory, link_name, test_settings, monkeypatch
) -> None:
    await _add_link(factory, link_name)
    pages = Pages(
        _page(),
        _page(status=503),
        CheckFailure("fetch: ConnectError: refused"),
        _page(),
    )
    monkeypatch.setattr(checker, "fetch", pages)

    result = await checker.check_link(factory, test_settings, link_name)
    assert result is not None and result.ok
    assert result.detail == "200 from www.academic.ntust.edu.tw"
    assert _sample("tigerduck_link_check_ok", name=link_name) == 1
    assert _failing_series(link_name) == {}

    await checker.check_link(factory, test_settings, link_name)
    row = await _row(factory, link_name)
    assert (row.alert_state, row.last_check_ok) == (ALERT_FAILING, False)
    assert row.last_check_detail == row.alert_detail == "status: expected 200, found 503"
    assert _sample("tigerduck_link_check_ok", name=link_name) == 0
    assert _failing_series(link_name) == {"status: expected 200, found 503": 1}

    # Still failing, for another reason: the row says so, the alert does not move.
    await checker.check_link(factory, test_settings, link_name)
    row = await _row(factory, link_name)
    assert row.alert_state == ALERT_FAILING
    assert row.last_check_detail == "fetch: ConnectError: refused"
    assert row.alert_detail == "status: expected 200, found 503"
    assert _failing_series(link_name) == {"status: expected 200, found 503": 1}

    # The series stays at 0, so the alert resolves with the labels it fired with.
    await checker.check_link(factory, test_settings, link_name)
    row = await _row(factory, link_name)
    assert (row.alert_state, row.last_check_ok) == (ALERT_OK, True)
    assert row.alert_detail == "status: expected 200, found 503"
    assert _sample("tigerduck_link_check_ok", name=link_name) == 1
    assert _failing_series(link_name) == {"status: expected 200, found 503": 0}
    assert _sample("tigerduck_link_check_last_timestamp", name=link_name) == (
        row.last_check_at.timestamp()
    )


@db_test
async def test_a_restart_exports_the_stored_alert_state_before_any_check(
    factory, link_name, test_settings, monkeypatch
) -> None:
    checked = datetime.now(UTC) - timedelta(minutes=10)
    await _add_link(
        factory,
        link_name,
        last_check_at=checked,
        last_check_ok=False,
        # The failure has changed since it started; the alert keeps the first.
        last_check_detail="fetch: ConnectError: refused",
        alert_state=ALERT_FAILING,
        alert_detail="status: expected 200, found 503",
    )
    _new_process(link_name)
    pages = Pages(_page(status=502), _page())
    monkeypatch.setattr(checker, "fetch", pages)

    # Checked 10 minutes ago, due in 60: the startup run only exports.
    await checker.run_link_checks(factory, test_settings)
    assert pages.urls == []
    assert _sample("tigerduck_link_check_ok", name=link_name) == 0
    assert _failing_series(link_name) == {"status: expected 200, found 503": 1}

    # A failing check after the restart is no new failure.
    await checker.check_link(factory, test_settings, link_name)
    row = await _row(factory, link_name)
    assert row.alert_state == ALERT_FAILING
    assert row.last_check_detail == "status: expected 200, found 502"
    assert _failing_series(link_name) == {"status: expected 200, found 503": 1}

    await checker.check_link(factory, test_settings, link_name)
    assert (await _row(factory, link_name)).alert_state == ALERT_OK
    assert _failing_series(link_name) == {"status: expected 200, found 503": 0}


@db_test
async def test_a_restart_right_after_a_recovery_exports_the_cleared_alert(
    factory, link_name, test_settings, monkeypatch
) -> None:
    await _add_link(factory, link_name)
    monkeypatch.setattr(checker, "fetch", Pages(_page(status=503), _page()))
    await checker.check_link(factory, test_settings, link_name)
    await checker.check_link(factory, test_settings, link_name)
    # Restarted before Prometheus scraped the 0.
    _new_process(link_name)

    await checker.run_link_checks(factory, test_settings)

    assert _sample("tigerduck_link_check_ok", name=link_name) == 1
    assert _failing_series(link_name) == {"status: expected 200, found 503": 0}


@db_test
async def test_a_disabled_failing_link_drops_its_failure_to_0_after_a_restart(
    factory, link_name, test_settings
) -> None:
    await _add_link(
        factory,
        link_name,
        enabled=False,
        last_check_at=datetime.now(UTC),
        alert_state=ALERT_FAILING,
        alert_detail="status: expected 200, found 503",
    )
    _new_process(link_name)

    await checker.run_link_checks(factory, test_settings)

    assert _sample("tigerduck_link_check_ok", name=link_name) is None
    assert _failing_series(link_name) == {"status: expected 200, found 503": 0}


@db_test
async def test_a_failing_link_edited_to_be_checked_again_keeps_alerting(
    factory, link_name, test_settings
) -> None:
    await _add_link(
        factory,
        link_name,
        last_check_at=None,
        alert_state=ALERT_FAILING,
        alert_detail="status: expected 200, found 503",
    )
    _new_process(link_name)

    checker.publish(await _row(factory, link_name))

    assert _sample("tigerduck_link_check_ok", name=link_name) == 0
    assert _sample("tigerduck_link_check_last_timestamp", name=link_name) is None
    assert _failing_series(link_name) == {"status: expected 200, found 503": 1}


@db_test
async def test_the_job_checks_only_enabled_links_that_are_due(
    factory, link_name, test_settings, monkeypatch
) -> None:
    now = datetime.now(UTC)
    due, fresh, off = f"{link_name}-due", f"{link_name}-fresh", f"{link_name}-off"
    await _add_link(factory, due, last_check_at=now - timedelta(minutes=61))
    await _add_link(factory, fresh, last_check_at=now - timedelta(minutes=59))
    await _add_link(
        factory, off, enabled=False, last_check_at=now - timedelta(days=1)
    )
    metrics.publish_link(off, ok=True, checked_at=0.0)
    pages = Pages(_page())
    monkeypatch.setattr(checker, "fetch", pages)

    try:
        assert await checker.run_link_checks(factory, test_settings) == 1
        assert (await _row(factory, due)).last_check_ok is True
        assert (await _row(factory, fresh)).last_check_ok is None
        # A disabled link's series go, so its alert resolves.
        assert _sample("tigerduck_link_check_ok", name=off) is None
        assert _sample("tigerduck_link_check_ok", name=fresh) == 1
    finally:
        for name in (due, fresh, off):
            metrics.forget_link(name)


@db_test
async def test_a_check_that_raises_fails_its_link_and_the_tick_goes_on(
    factory, link_name, test_settings, monkeypatch
) -> None:
    older, never = f"{link_name}-older", f"{link_name}-never"
    await _add_link(factory, older, last_check_at=datetime.now(UTC) - timedelta(days=1))
    # Added last, checked first: a link never checked goes ahead of the others.
    await _add_link(factory, never)
    urls: list[str] = []

    def raises_once(url, allowed_hosts):
        urls.append(url)
        if len(urls) == 1:
            raise httpx.InvalidURL("Invalid non-printable ASCII character in URL")
        return _page()

    monkeypatch.setattr(checker, "fetch", raises_once)

    try:
        assert await checker.run_link_checks(factory, test_settings) == 2
        row = await _row(factory, never)
        assert (row.last_check_ok, row.alert_state) == (False, ALERT_FAILING)
        assert row.last_check_detail == (
            "fetch: InvalidURL: Invalid non-printable ASCII character in URL"
        )
        assert row.last_check_at is not None
        assert (await _row(factory, older)).last_check_ok is True
    finally:
        _new_process(older)
        _new_process(never)


@db_test
async def test_a_link_edited_during_its_check_records_nothing(
    factory, link_name, test_settings, monkeypatch
) -> None:
    await _add_link(factory, link_name)

    async def edited_meanwhile(target_url, expectations, allowed_hosts):
        async with factory() as session:
            await session.execute(
                update(Link)
                .where(Link.name == link_name)
                .values(target_url="https://www.ntust.edu.tw/")
            )
            await session.commit()
        return checker.CheckResult(False, "status: expected 200, found 404")

    monkeypatch.setattr(checker, "run_check", edited_meanwhile)

    assert await checker.check_link(factory, test_settings, link_name) is None
    row = await _row(factory, link_name)
    assert (row.last_check_at, row.alert_state) == (None, ALERT_OK)


@db_test
async def test_a_check_that_ends_after_a_newer_one_does_not_replace_its_result(
    factory, link_name, test_settings, monkeypatch
) -> None:
    import asyncio

    await _add_link(factory, link_name)
    slow_started, release = asyncio.Event(), asyncio.Event()
    calls: list[int] = []

    async def fake_run_check(target_url, expectations, allowed_hosts):
        calls.append(len(calls))
        if len(calls) == 1:
            slow_started.set()
            await release.wait()
            return checker.CheckResult(False, "status: expected 200, found 503")
        return checker.CheckResult(True, "200 from www.academic.ntust.edu.tw")

    monkeypatch.setattr(checker, "run_check", fake_run_check)
    slow = asyncio.create_task(checker.check_link(factory, test_settings, link_name))
    await slow_started.wait()

    newer = await checker.check_link(factory, test_settings, link_name)
    release.set()
    older = await slow

    assert (newer.ok, older.ok) == (True, False)
    row = await _row(factory, link_name)
    assert (row.last_check_ok, row.alert_state) == (True, ALERT_OK)
    assert row.last_check_detail == "200 from www.academic.ntust.edu.tw"


@db_test
async def test_a_check_runs_the_fetch_in_a_thread_with_the_allowed_hosts(
    factory, link_name, test_settings, monkeypatch
) -> None:
    import threading

    seen = {}

    def fake_fetch(url, allowed_hosts):
        seen["thread"] = threading.current_thread()
        seen["allowed"] = allowed_hosts
        return _page()

    monkeypatch.setattr(checker, "fetch", fake_fetch)
    await _add_link(factory, link_name)

    await checker.check_link(factory, test_settings, link_name)

    assert seen["thread"] is not threading.main_thread()
    assert seen["allowed"] == test_settings.links_allowed_hosts


@pytest.mark.parametrize(("tick", "scheduled"), [(60, True), (0, False)])
def test_the_scheduler_runs_the_checker_at_startup_unless_turned_off(
    test_settings, tick: int, scheduled: bool
) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from server.bulletins.llm.base import BulletinMetadata
    from server.bulletins.llm.openai_compat import RecordingProvider
    from server.bulletins.taxonomy import CanonicalOrg
    from server.scheduler.runtime import build_scheduler

    before = datetime.now(UTC)
    scheduler = build_scheduler(
        async_sessionmaker(),
        None,
        test_settings.model_copy(update={"link_check_tick_seconds": tick}),
        llm=RecordingProvider(BulletinMetadata(canonical_org=CanonicalOrg.other)),
    )

    job = scheduler.get_job("link_check")
    assert (job is not None) == scheduled
    if job is not None:
        assert job.trigger.interval == timedelta(seconds=60)
        assert before <= job.next_run_time <= datetime.now(UTC)
