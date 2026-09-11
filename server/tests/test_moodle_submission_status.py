"""Submission-status probe — driven through a stub transport, never the network."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest
import structlog

from server.syncjobs import moodle_client
from server.syncjobs.moodle_client import (
    HttpAssignmentFetcher,
    MoodleRateLimited,
    MoodleTokenInvalid,
    MoodleUnreachable,
)

BASE = "https://moodle.example.test"


def _transport(handler):
    return httpx.MockTransport(handler)


def _site_info(userid: int = 42) -> dict:
    return {"userid": userid, "sitename": "test"}


def _status(submitted: bool, when: int | None) -> dict:
    plugins: list[dict] = []
    return {
        "lastattempt": {
            "submission": {
                "status": "submitted" if submitted else "new",
                "timemodified": when or 0,
                "plugins": plugins,
            }
        }
    }


def _handler_for(statuses: dict[int, dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        fn = request.url.params.get("wsfunction")
        if fn == "core_webservice_get_site_info":
            return httpx.Response(200, json=_site_info())
        if fn == "mod_assign_get_submission_status":
            assignid = int(request.url.params["assignid"])
            return httpx.Response(200, json=statuses[assignid])
        raise AssertionError(f"unexpected wsfunction {fn}")

    return handler


@pytest.mark.asyncio
async def test_reports_submitted_and_unsubmitted():
    fetcher = HttpAssignmentFetcher(
        base_url=BASE,
        timeout_seconds=5.0,
        transport=_transport(
            _handler_for(
                {
                    1: _status(True, 1_757_000_000),
                    2: _status(False, None),
                }
            )
        ),
    )

    result = await fetcher.fetch_submission_status(
        token="tok", assignment_ids=[1, 2], max_concurrency=2
    )

    assert result[1].is_submitted is True
    assert result[1].submitted_at == datetime.fromtimestamp(1_757_000_000, tz=UTC)
    assert result[2].is_submitted is False
    assert result[2].submitted_at is None


@pytest.mark.asyncio
async def test_one_failing_assignment_does_not_lose_the_others():
    def handler(request: httpx.Request) -> httpx.Response:
        fn = request.url.params.get("wsfunction")
        if fn == "core_webservice_get_site_info":
            return httpx.Response(200, json=_site_info())
        assignid = int(request.url.params["assignid"])
        if assignid == 2:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json=_status(True, 1_757_000_000))

    fetcher = HttpAssignmentFetcher(
        base_url=BASE, timeout_seconds=5.0, transport=_transport(handler)
    )

    result = await fetcher.fetch_submission_status(
        token="tok", assignment_ids=[1, 2, 3], max_concurrency=3
    )

    # The broken one is simply absent — callers leave those rows untouched.
    assert set(result) == {1, 3}


@pytest.mark.asyncio
async def test_one_timing_out_assignment_does_not_lose_the_others():
    """Regression for I1 (task-1-review.md): a transport failure -- not
    just a >= 400 status -- on one probe must not abort the batch.

    This needs a semaphore narrower than the batch (so later ids are
    still queued, not yet dispatched, when the timeout fires) and a
    handler that genuinely yields (`await asyncio.sleep(0)`, as real
    network I/O always does). With only 3 ids and a synchronous handler,
    every probe races to completion inside one scheduling step before the
    `ReadTimeout` propagates, so the pre-fix code already holds the same
    result the fix would produce and the test cannot tell them apart --
    that was N2 (task-1-fix-1-rereview.md): this test previously used
    those parameters and passed against the unfixed client too.

    Measured with 6 ids, `max_concurrency=2` and this async handler: the
    pre-fix client returns only `{1, 3, 4}` -- ids 5 and 6 are still
    waiting on the semaphore when the `ReadTimeout` on id 2 aborts
    `asyncio.gather`, so their requests are never even sent. The fix must
    return every surviving id.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        fn = request.url.params.get("wsfunction")
        if fn == "core_webservice_get_site_info":
            return httpx.Response(200, json=_site_info())
        assignid = int(request.url.params["assignid"])
        await asyncio.sleep(0)
        if assignid == 2:
            raise httpx.ReadTimeout("timed out")
        return httpx.Response(200, json=_status(True, 1_757_000_000))

    fetcher = HttpAssignmentFetcher(
        base_url=BASE, timeout_seconds=5.0, transport=_transport(handler)
    )

    result = await fetcher.fetch_submission_status(
        token="tok", assignment_ids=[1, 2, 3, 4, 5, 6], max_concurrency=2
    )

    # The timed-out probe is simply absent -- every other id, including
    # the ones still queued behind the narrow semaphore, must land.
    assert set(result) == {1, 3, 4, 5, 6}


@pytest.mark.asyncio
async def test_invalid_token_propagates():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"exception": "moodle_exception", "errorcode": "invalidtoken"}
        )

    fetcher = HttpAssignmentFetcher(
        base_url=BASE, timeout_seconds=5.0, transport=_transport(handler)
    )

    with pytest.raises(MoodleTokenInvalid):
        await fetcher.fetch_submission_status(
            token="tok", assignment_ids=[1], max_concurrency=1
        )


@pytest.mark.asyncio
async def test_invalid_token_from_a_probe_propagates_unwrapped():
    """N3 (task-1-fix-1-rereview.md): `test_invalid_token_propagates`
    above only exercises the token check inside `_get_moodle_userid`,
    which raises *before* the `TaskGroup` is even entered. It never
    touches the `except* MoodleTokenInvalid` unwrap that the batch relies
    on. Here the token dies on a probe, inside the group, so a regression
    that let the bare `ExceptionGroup` escape instead of unwrapping it
    would actually be caught.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        fn = request.url.params.get("wsfunction")
        if fn == "core_webservice_get_site_info":
            return httpx.Response(200, json=_site_info())
        assignid = int(request.url.params["assignid"])
        if assignid == 2:
            return httpx.Response(
                200,
                json={"exception": "moodle_exception", "errorcode": "invalidtoken"},
            )
        return httpx.Response(200, json=_status(True, 1_757_000_000))

    fetcher = HttpAssignmentFetcher(
        base_url=BASE, timeout_seconds=5.0, transport=_transport(handler)
    )

    with pytest.raises(MoodleTokenInvalid) as excinfo:
        await fetcher.fetch_submission_status(
            token="tok", assignment_ids=[1, 2, 3], max_concurrency=3
        )

    assert not isinstance(excinfo.value, BaseExceptionGroup)


@pytest.mark.asyncio
async def test_mixed_token_and_rate_limit_batch_picks_token():
    """N1 (task-1-fix-1-rereview.md): `except*` runs *every* matching
    clause, so a batch with one dead-token probe and one rate-limited
    probe used to fire both clauses and recombine their raises into a
    bare `ExceptionGroup` that matched neither this module's own outer
    `except MoodleTokenInvalid` / `except MoodleRateLimited` arms nor the
    executor's -- it fell into the catch-all and was filed as
    `sync_failed:ExceptionGroup`, silently skipping both re-auth and
    rate-limit backoff.

    The fix picks one error deliberately instead of letting the group
    through: a dead token makes the rate limit moot (we must stop using
    the credential either way) and only the token error triggers
    re-auth, so `MoodleTokenInvalid` wins. This must hold as a plain
    exception, not a group.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        fn = request.url.params.get("wsfunction")
        if fn == "core_webservice_get_site_info":
            return httpx.Response(200, json=_site_info())
        assignid = int(request.url.params["assignid"])
        await asyncio.sleep(0)
        if assignid == 1:
            return httpx.Response(
                200,
                json={"exception": "moodle_exception", "errorcode": "invalidtoken"},
            )
        if assignid == 2:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, json=_status(True, 1_757_000_000))

    fetcher = HttpAssignmentFetcher(
        base_url=BASE, timeout_seconds=5.0, transport=_transport(handler)
    )

    with pytest.raises(MoodleTokenInvalid) as excinfo:
        await fetcher.fetch_submission_status(
            token="tok", assignment_ids=[1, 2, 3], max_concurrency=3
        )

    assert not isinstance(excinfo.value, BaseExceptionGroup)


@pytest.mark.asyncio
async def test_rate_limited_probe_raises():
    """I3 (task-1-review.md): a 429 on a per-assignment probe must become
    `MoodleRateLimited`, the same contract `fetch_assignments` follows --
    not be folded into the generic >= 400 skip-and-continue path, which
    left the batch silently returning `{}` while still hammering Moodle.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        fn = request.url.params.get("wsfunction")
        if fn == "core_webservice_get_site_info":
            return httpx.Response(200, json=_site_info())
        return httpx.Response(429, text="slow down")

    fetcher = HttpAssignmentFetcher(
        base_url=BASE, timeout_seconds=5.0, transport=_transport(handler)
    )

    with pytest.raises(MoodleRateLimited):
        await fetcher.fetch_submission_status(
            token="tok", assignment_ids=[1, 2, 3], max_concurrency=3
        )


@pytest.mark.asyncio
async def test_rate_limited_site_info_raises():
    """I3: the site-info call (`_get_moodle_userid`) obeys the same 429
    contract -- previously it checked no status code at all, so a 429
    with a non-JSON body surfaced as a swallowed `JSONDecodeError`.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="slow down")

    fetcher = HttpAssignmentFetcher(
        base_url=BASE, timeout_seconds=5.0, transport=_transport(handler)
    )

    with pytest.raises(MoodleRateLimited):
        await fetcher.fetch_submission_status(
            token="tok", assignment_ids=[1], max_concurrency=1
        )


@pytest.mark.asyncio
async def test_empty_input_makes_no_requests():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params.get("wsfunction", ""))
        return httpx.Response(200, json=_site_info())

    fetcher = HttpAssignmentFetcher(
        base_url=BASE, timeout_seconds=5.0, transport=_transport(handler)
    )

    result = await fetcher.fetch_submission_status(
        token="tok", assignment_ids=[], max_concurrency=4
    )

    assert result == {}
    # Not even the site-info call: no assignments means no work at all.
    assert calls == []


@pytest.mark.asyncio
async def test_token_never_appears_in_logs(capsys, caplog):
    """I2 (task-1-review.md): the client logs through structlog's
    `PrintLoggerFactory`, which writes to stdout via a bare `print()` --
    it never becomes a stdlib `LogRecord`, so `caplog` cannot see it. The
    previous version of this test asserted only on `caplog.text`, which
    was already empty before the call, so a logger that actively leaked
    the token still passed it (verified by hand: wrapping the client's
    logger to attach the token to the warning left this assertion green).

    Capture what the code actually writes with `capsys`, and exercise the
    paths that could plausibly leak -- a transport error and a non-2xx
    status -- alongside a successful probe, not just the happy path.
    `caplog` is still asserted too, as cheap insurance against a future
    path that logs through the stdlib `logging` module instead.
    """
    token = "super-secret-token"

    def handler(request: httpx.Request) -> httpx.Response:
        fn = request.url.params.get("wsfunction")
        if fn == "core_webservice_get_site_info":
            return httpx.Response(200, json=_site_info())
        assignid = int(request.url.params["assignid"])
        if assignid == 1:
            raise httpx.ConnectError("refused")
        if assignid == 2:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json=_status(True, 1_757_000_000))

    fetcher = HttpAssignmentFetcher(
        base_url=BASE, timeout_seconds=5.0, transport=_transport(handler)
    )

    with caplog.at_level("DEBUG"):
        result = await fetcher.fetch_submission_status(
            token=token, assignment_ids=[1, 2, 3], max_concurrency=3
        )

    # Sanity check that the scenario actually drove all three paths: the
    # transport error (1) and the 500 (2) are absent, the success (3) lands.
    assert set(result) == {3}

    stdout = capsys.readouterr().out
    assert token not in stdout
    assert token not in caplog.text


@pytest.mark.asyncio
async def test_malformed_timestamp_does_not_lose_the_batch():
    """C1 (task-1-fix-2-rereview.md): `_parse_submission_status` used to be
    called *outside* `probe`'s guarded `try`, so a bogus `timemodified` --
    ordinary malformed data, not a contrived input -- raised straight out of
    `probe` unmatched by any `except*` clause and escaped
    `fetch_submission_status` as a bare `ExceptionGroup`, taking the whole
    batch down with it.

    A `timemodified` this large overflows `datetime.fromtimestamp`'s
    platform `time_t`: `OverflowError`, not `ValueError` -- it descends from
    `ArithmeticError`, so widening `except ValueError` alone would still
    miss it. `json.loads` (what `httpx.Response.json()` uses) accepts a
    Python int of any size, so Moodle sending this is not far-fetched.

    The malformed row must be skipped like every other broken probe, not
    lose the rest of the batch.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        fn = request.url.params.get("wsfunction")
        if fn == "core_webservice_get_site_info":
            return httpx.Response(200, json=_site_info())
        assignid = int(request.url.params["assignid"])
        if assignid == 2:
            return httpx.Response(200, json=_status(True, 10**20))
        return httpx.Response(200, json=_status(True, 1_757_000_000))

    fetcher = HttpAssignmentFetcher(
        base_url=BASE, timeout_seconds=5.0, transport=_transport(handler)
    )

    result = await fetcher.fetch_submission_status(
        token="tok", assignment_ids=[1, 2, 3], max_concurrency=3
    )

    # The malformed one is simply absent -- same contract as every other
    # broken probe.
    assert set(result) == {1, 3}


class _UnhashableAssignmentId(int):
    """An `int` a dict will not accept as a key.

    Stand-in for "some statement in `probe` that is not inside the inner
    guard raises". It is a genuine `int`, so it satisfies the
    `assignment_ids: list[int]` annotation and formats into the request
    params like any other id; the single thing it changes is that
    `results[assignment_id] = parsed` raises `TypeError`.
    """

    __hash__ = None


class _LoggerThatFailsOnWarning:
    """A `logger` whose `warning` raises, delegating everything else.

    Not synthetic: `logging_setup.py` configures structlog with
    `PrintLoggerFactory()`, so every line is a plain write to stdout and a
    broken pipe there raises `OSError` -- an ordinary `Exception`, raised
    from inside a probe's own `except` clause, where no guard used to reach.
    """

    def __init__(self, real):
        self._real = real

    def warning(self, *args, **kwargs):
        raise OSError("broken pipe writing the log line")

    def __getattr__(self, name):
        return getattr(self._real, name)


# Every entry is a *different statement inside `probe`*, walking its whole
# body: the request, the response read, the parse, the result store after
# the inner guard, and the inner guard's own handler.
_PROBE_INJECTION_SITES = (
    "before_the_request",
    "during_the_response_read",
    "inside_the_parse",
    "at_the_result_store",
    "inside_the_skip_handler",
)

_INJECTED_MESSAGE = "nobody wrote an except* clause for this"

# Assignments 1 and 3 answer slowly so they are demonstrably *still in
# flight* when assignment 2 fails. Without this the stub transport answers
# every probe in microseconds, all three finish before anything can be
# cancelled, and the `set(result) == {1, 3}` half of the invariant holds
# even for a fix that lets the exception reach the TaskGroup -- measured:
# a residual `except* Exception` arm at the group passes the whole
# parametrization with an instant transport and returns `{}` with this
# delay, because a TaskGroup aborts its siblings. A sleeping task cannot
# finish early, so a loaded machine cannot turn this into a false failure.
_SIBLING_DELAY_SECONDS = 0.05


def _install_probe_injection(site: str, monkeypatch):
    """Arrange for assignment 2's probe to fail at `site`.

    Returns the `(handler, assignment_ids)` to drive the fetcher with.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        fn = request.url.params.get("wsfunction")
        if fn == "core_webservice_get_site_info":
            return httpx.Response(200, json=_site_info())
        assignid = int(request.url.params["assignid"])
        if assignid != 2:
            await asyncio.sleep(_SIBLING_DELAY_SECONDS)
        if assignid == 2 and site == "before_the_request":
            # MockTransport calls this handler from inside `client.get`, so
            # an arbitrary exception here surfaces exactly where a transport
            # would raise one.
            raise RuntimeError(_INJECTED_MESSAGE)
        if assignid == 2 and site == "inside_the_skip_handler":
            # A `timemodified` that overflows `time_t` routes probe 2 into
            # the skip handler, whose own `logger.warning` then raises.
            return httpx.Response(200, json=_status(True, 10**20))
        return httpx.Response(200, json=_status(True, 1_757_000_000))

    if site == "during_the_response_read":
        real_json = httpx.Response.json

        def _json_or_boom(self, **kwargs):
            # Only the probe requests carry `assignid`; site-info still
            # parses normally, so the batch gets as far as `probe`.
            if self.request.url.params.get("assignid") == "2":
                raise RuntimeError(_INJECTED_MESSAGE)
            return real_json(self, **kwargs)

        monkeypatch.setattr(httpx.Response, "json", _json_or_boom)

    if site == "inside_the_parse":
        real_parse = moodle_client._parse_submission_status

        def _parse_or_boom(assignment_id: int, body: dict):
            if assignment_id == 2:
                raise RuntimeError(_INJECTED_MESSAGE)
            return real_parse(assignment_id, body)

        monkeypatch.setattr(
            moodle_client, "_parse_submission_status", _parse_or_boom
        )

    if site == "inside_the_skip_handler":
        monkeypatch.setattr(
            moodle_client,
            "logger",
            _LoggerThatFailsOnWarning(moodle_client.logger),
        )

    ids: list[int] = [1, 2, 3]
    if site == "at_the_result_store":
        ids = [1, _UnhashableAssignmentId(2), 3]
    return handler, ids


@pytest.mark.parametrize("site", _PROBE_INJECTION_SITES)
@pytest.mark.asyncio
async def test_unexpected_probe_exception_never_escapes_as_a_group(
    site, monkeypatch
):
    """M1+M2 (task-1-fix-3-rereview.md): the invariant, generalized over
    *location* as well as type.

        an arbitrary exception raised anywhere inside a probe never reaches
        the caller as a BaseExceptionGroup, and never costs its siblings

    Rounds 1 and 2 each fixed one *exception type* that recombined into a
    bare `ExceptionGroup`; round 3 closed the type axis by injecting an
    arbitrary `RuntimeError` -- but at exactly one call site, inside the
    guarded `try`. Two statements in `probe` sat outside that guard
    (`results[assignment_id] = parsed`, and the `logger` calls in the
    handlers themselves) and still escaped as groups while that test stayed
    green. A test that generalizes on one axis and not the other looks like
    a class-level test and is not one, so this one parametrizes over the
    body of `probe`: fail if *any* statement in it can escape.

    Not just "no group escaped": a TaskGroup cancels every sibling the
    moment one member raises, so an escape also silently costs assignments
    1 and 3. Both halves are asserted.

    `BaseException` is out of scope on purpose -- `CancelledError` must
    keep escaping as a group rather than becoming a skipped assignment.
    """
    handler, ids = _install_probe_injection(site, monkeypatch)

    fetcher = HttpAssignmentFetcher(
        base_url=BASE, timeout_seconds=5.0, transport=_transport(handler)
    )

    try:
        result = await fetcher.fetch_submission_status(
            token="tok", assignment_ids=ids, max_concurrency=3
        )
    except BaseException as exc:
        assert not isinstance(exc, BaseExceptionGroup), (
            f"a probe exception injected {site} escaped as {exc!r} instead "
            "of being handled inside the probe"
        )
        raise

    # The failing probe is skipped, and only it. Assignments 1 and 3 are
    # still awaiting their response when 2 fails, so if the exception had
    # reached the TaskGroup they would have been cancelled and lost -- this
    # is the half that "handled somewhere, no group escaped" does not cover.
    assert set(result) == {1, 3}, (
        f"an exception injected {site} did not stay confined to its own "
        f"assignment: expected {{1, 3}}, got {set(result)}"
    )


def test_skip_tuple_can_never_swallow_an_escalation():
    """M1 (task-1-fix-3-rereview.md), structural half.

    Consolidating `probe`'s guards left one ordering hazard: the inner
    `except _PROBE_SKIPS` clause runs before the outer `except
    _PROBE_ESCALATES`, so if any escalating type were ever a subclass of a
    skippable one it would be logged as routine probe noise and the
    assignment silently skipped instead of the batch stopping. Nothing
    enforces that at the two `except` sites, so it is enforced here.
    """
    for escalating in moodle_client._PROBE_ESCALATES:
        assert not issubclass(escalating, moodle_client._PROBE_SKIPS), (
            f"{escalating.__name__} is in _PROBE_ESCALATES but would be "
            "caught by the skip clause first and never escalate"
        )


@pytest.mark.asyncio
async def test_moodle_unreachable_from_a_probe_propagates_unwrapped(monkeypatch):
    """M3 (task-1-fix-3-rereview.md): the broad residual clause must not eat
    this module's own error taxonomy.

    `MoodleUnreachable` is one of the three types `executor.py::_execute_job`
    branches on (`:373` -> retriable backoff). Round 3's `except Exception`
    caught it, logged it as *unexpected*, and skipped the assignment, so it
    never reached that branch. Nothing inside `probe` raises it today --
    Plan C Tasks 2 and 3 wire this function up and will reasonably expect
    this module's own types to behave here the way they do everywhere else
    in the file.

    It must arrive plain, never inside a group, with the group still
    reachable through `__cause__`.
    """

    real_parse = moodle_client._parse_submission_status

    def _parse_or_unreachable(assignment_id: int, body: dict):
        if assignment_id == 2:
            raise MoodleUnreachable("probe_backend_down")
        return real_parse(assignment_id, body)

    monkeypatch.setattr(
        moodle_client, "_parse_submission_status", _parse_or_unreachable
    )

    def handler(request: httpx.Request) -> httpx.Response:
        fn = request.url.params.get("wsfunction")
        if fn == "core_webservice_get_site_info":
            return httpx.Response(200, json=_site_info())
        return httpx.Response(200, json=_status(True, 1_757_000_000))

    fetcher = HttpAssignmentFetcher(
        base_url=BASE, timeout_seconds=5.0, transport=_transport(handler)
    )

    with pytest.raises(MoodleUnreachable) as excinfo:
        await fetcher.fetch_submission_status(
            token="tok", assignment_ids=[1, 2, 3], max_concurrency=3
        )

    assert not isinstance(excinfo.value, BaseExceptionGroup)
    # The discarded detail is not lost: `from eg` keeps the original group.
    assert isinstance(excinfo.value.__cause__, BaseExceptionGroup)
    assert excinfo.value.__cause__.subgroup(MoodleUnreachable) is not None


@pytest.mark.parametrize(
    ("winner", "loser_payload"),
    [
        (MoodleTokenInvalid, "invalidtoken"),
        (MoodleRateLimited, "http_429"),
    ],
)
@pytest.mark.asyncio
async def test_unreachable_loses_to_the_more_specific_escalations(
    winner, loser_payload, monkeypatch
):
    """M3, priority half: adding `MoodleUnreachable` to the escalation set
    gives the `except*` router three types to choose between, and `except*`
    hands it all of them at once.

    A dead token makes everything else moot (only it triggers re-auth) and a
    429 is a specific instruction from the server, so both outrank the
    generic transient. Getting this wrong would file a dead credential as
    `sync_failed:submission_probe_unreachable` and never disable the job.

    Both probes are held at a barrier so they are past their last suspension
    point before either raises -- otherwise the TaskGroup would cancel the
    second one and the batch would only ever contain one escalation.
    """
    barrier = asyncio.Barrier(2)
    real_parse = moodle_client._parse_submission_status

    def _parse_or_escalate(assignment_id: int, body: dict):
        if assignment_id == 1:
            raise MoodleUnreachable("probe_backend_down")
        return real_parse(assignment_id, body)

    monkeypatch.setattr(
        moodle_client, "_parse_submission_status", _parse_or_escalate
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        fn = request.url.params.get("wsfunction")
        if fn == "core_webservice_get_site_info":
            return httpx.Response(200, json=_site_info())
        assignid = int(request.url.params["assignid"])
        if assignid in (1, 2):
            await barrier.wait()
        if assignid == 2:
            if loser_payload == "http_429":
                return httpx.Response(429, text="slow down")
            return httpx.Response(
                200,
                json={"exception": "moodle_exception", "errorcode": loser_payload},
            )
        return httpx.Response(200, json=_status(True, 1_757_000_000))

    fetcher = HttpAssignmentFetcher(
        base_url=BASE, timeout_seconds=5.0, transport=_transport(handler)
    )

    with pytest.raises(winner) as excinfo:
        await fetcher.fetch_submission_status(
            token="tok", assignment_ids=[1, 2, 3], max_concurrency=3
        )

    assert not isinstance(excinfo.value, BaseExceptionGroup)
    # Both really were in the group -- otherwise this asserts nothing about
    # priority, only that the surviving one was routed.
    cause = excinfo.value.__cause__
    assert isinstance(cause, BaseExceptionGroup)
    assert cause.subgroup(MoodleUnreachable) is not None
    assert cause.subgroup(winner) is not None


@pytest.mark.asyncio
async def test_unexpected_probe_error_logs_a_traceback_without_the_token(
    capsys, monkeypatch
):
    """M4 (task-1-fix-3-rereview.md): `submission_probe_unexpected_error` is
    the one signal that says a new failure mode has appeared inside `probe`.
    With only `type(exc).__name__` it says a `RuntimeError` happened
    somewhere in the probe and nothing more, which is not enough to act on.

    The reason it was omitted is the reason it needs a test: the probe's own
    frame holds the token, in `token` and in the `params` dict, and the
    never-log-a-token rule is absolute. A rendered traceback shows source
    *lines*, not frame locals, so `exc_info` is safe -- asserted here
    against the real production processor chain
    (`logging_setup.configure`'s non-development branch: `format_exc_info`
    then `JSONRenderer`), not against whatever structlog happens to default
    to under pytest. The module's `logger` is a lazy proxy that caches its
    bound logger on first use, and structlog's *default* config sets
    `cache_logger_on_first_use=True`, so by the time this test runs an
    earlier test in this file has already frozen the console renderer onto
    it; configuring alone silently does nothing. The proxy is therefore
    replaced with one created after `configure`, and the assertions parse
    the line as JSON so a test running against the wrong chain fails
    instead of passing for the wrong reason.
    """
    token = "super-secret-token"
    real_parse = moodle_client._parse_submission_status

    def _parse_or_boom(assignment_id: int, body: dict):
        if assignment_id == 2:
            raise RuntimeError("unexpected probe failure")
        return real_parse(assignment_id, body)

    monkeypatch.setattr(moodle_client, "_parse_submission_status", _parse_or_boom)

    def handler(request: httpx.Request) -> httpx.Response:
        fn = request.url.params.get("wsfunction")
        if fn == "core_webservice_get_site_info":
            return httpx.Response(200, json=_site_info())
        return httpx.Response(200, json=_status(True, 1_757_000_000))

    fetcher = HttpAssignmentFetcher(
        base_url=BASE, timeout_seconds=5.0, transport=_transport(handler)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )
    try:
        monkeypatch.setattr(
            moodle_client,
            "logger",
            structlog.get_logger("server.syncjobs.moodle_client"),
        )
        result = await fetcher.fetch_submission_status(
            token=token, assignment_ids=[1, 2, 3], max_concurrency=3
        )
    finally:
        structlog.reset_defaults()

    assert set(result) == {1, 3}
    stdout = capsys.readouterr().out
    lines = [
        line
        for line in stdout.splitlines()
        if "submission_probe_unexpected_error" in line
    ]
    assert len(lines) == 1, f"expected one unexpected-error line, got {lines}"
    # Parsing as JSON is the proof that the production chain really rendered
    # this line -- structlog's default ConsoleRenderer would not produce it.
    payload = json.loads(lines[0])
    assert payload["event"] == "syncjobs.moodle.submission_probe_unexpected_error"
    assert payload["level"] == "error"
    assert payload["error"] == "RuntimeError"
    # The traceback is the point: it must name the frame the exception came
    # from, not merely the exception's type.
    traceback_text = payload["exception"]
    assert traceback_text.startswith("Traceback (most recent call last)")
    assert "_parse_or_boom" in traceback_text
    # ... and it must still carry nothing that identifies the credential,
    # even though the probe frame it walks through holds the token both as a
    # closure variable and inside its `params` dict.
    assert token not in stdout
    assert "wstoken" not in stdout
