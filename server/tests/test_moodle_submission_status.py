"""Submission-status probe — driven through a stub transport, never the network."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

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

    Before the fix, `probe` only guarded the `>= 400` case; an
    `httpx.ReadTimeout` (or a non-JSON body) propagated straight out of
    `asyncio.gather(..., return_exceptions=False)`. The reviewer measured
    this by hand: with ids [1, 2, 3] and a timeout on one of them, the
    committed code returned only the assignment requested *before* the
    timeout -- the request after it was never even sent.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        fn = request.url.params.get("wsfunction")
        if fn == "core_webservice_get_site_info":
            return httpx.Response(200, json=_site_info())
        assignid = int(request.url.params["assignid"])
        if assignid == 2:
            raise httpx.ReadTimeout("timed out")
        return httpx.Response(200, json=_status(True, 1_757_000_000))

    fetcher = HttpAssignmentFetcher(
        base_url=BASE, timeout_seconds=5.0, transport=_transport(handler)
    )

    result = await fetcher.fetch_submission_status(
        token="tok", assignment_ids=[1, 2, 3], max_concurrency=3
    )

    # The timed-out probe is simply absent -- the others must still land.
    assert set(result) == {1, 3}


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
