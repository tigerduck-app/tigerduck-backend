"""Submission-status probe — driven through a stub transport, never the network."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from server.syncjobs.moodle_client import (
    HttpAssignmentFetcher,
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
async def test_token_never_appears_in_logs(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    fetcher = HttpAssignmentFetcher(
        base_url=BASE, timeout_seconds=5.0, transport=_transport(handler)
    )

    with caplog.at_level("WARNING"):
        result = await fetcher.fetch_submission_status(
            token="super-secret-token", assignment_ids=[1], max_concurrency=1
        )

    assert result == {}
    assert "super-secret-token" not in caplog.text
