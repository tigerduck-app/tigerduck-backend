"""Submission-status probe — driven through a stub transport, never the network."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from server.syncjobs import moodle_client
from server.syncjobs.moodle_client import (
    HttpAssignmentFetcher,
    MoodleRateLimited,
    MoodleTokenInvalid,
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


@pytest.mark.asyncio
async def test_unexpected_probe_exception_never_escapes_as_a_group(monkeypatch):
    """C1 (task-1-fix-2-rereview.md), generalized: round 1 fixed exactly two
    named exception types recombining into a bare `ExceptionGroup`
    (`test_mixed_token_and_rate_limit_batch_picks_token`). Fix round 3 fixed
    a third, the malformed-timestamp `OverflowError`/`ValueError` pair above.
    Neither closes the shape: `except*` runs every matching clause and lets
    anything unmatched escape as a residual group. This is the third time
    that shape has bitten this branch, so this test does not name a fourth
    exception type -- it pins the invariant that makes the *type* irrelevant:

        an arbitrary exception raised from inside a probe never reaches the
        caller as a BaseExceptionGroup.

    `RuntimeError` is injected via monkeypatch, not a crafted Moodle
    payload, because the point is the guard around `probe` itself, not any
    one bad response shape -- that is what the malformed-timestamp test
    above already covers. This test must keep passing no matter what future
    code runs inside `probe` or what it raises.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        fn = request.url.params.get("wsfunction")
        if fn == "core_webservice_get_site_info":
            return httpx.Response(200, json=_site_info())
        return httpx.Response(200, json=_status(True, 1_757_000_000))

    real_parse = moodle_client._parse_submission_status

    def _parse_or_boom(assignment_id: int, body: dict):
        if assignment_id == 2:
            raise RuntimeError("nobody wrote an except* clause for this")
        return real_parse(assignment_id, body)

    monkeypatch.setattr(moodle_client, "_parse_submission_status", _parse_or_boom)

    fetcher = HttpAssignmentFetcher(
        base_url=BASE, timeout_seconds=5.0, transport=_transport(handler)
    )

    try:
        result = await fetcher.fetch_submission_status(
            token="tok", assignment_ids=[1, 2, 3], max_concurrency=3
        )
    except BaseException as exc:
        # Whatever the decision (task-1-fix-3-report.md), reaching here as
        # a bare group is never it: it means `except*` matched nothing,
        # discarded no information on purpose, and let a residual group
        # through unrouted.
        assert not isinstance(exc, BaseExceptionGroup), (
            f"an unmatched probe exception escaped as {exc!r} instead of "
            "being handled inside the probe"
        )
        raise

    # Chosen contract: an exception no clause names is this assignment's
    # problem alone, same as every other single-probe failure -- skipped,
    # not escalated (see task-1-fix-3-report.md for why). Assert the batch
    # actually completed instead of merely "no exception happened to
    # propagate".
    assert set(result) == {1, 3}
