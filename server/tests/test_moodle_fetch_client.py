"""HttpAssignmentFetcher / HttpTokenObtainer against httpx.MockTransport."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from server.syncjobs.moodle_client import (
    HttpAssignmentFetcher,
    HttpTokenObtainer,
    MoodleRateLimited,
    MoodleTokenInvalid,
    MoodleUnreachable,
    SsoAuthFailed,
    SsoUnavailable,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

BASE = "https://moodle.example.edu"

ASSIGNMENTS_BODY = {
    "courses": [
        {
            "id": 7001,
            "fullname": "資料結構",
            "shortname": "CS2006301",
            "assignments": [
                {
                    "id": 555,
                    "cmid": 9001,
                    "course": 7001,
                    "name": "HW3",
                    "duedate": 1765400400,
                    "allowsubmissionsfromdate": 1764800400,
                    "cutoffdate": 0,
                    "intro": "<p>do it</p>",
                },
                {
                    "id": 556,
                    "cmid": 9002,
                    "course": 7001,
                    "name": "HW4",
                    "duedate": 0,
                    "allowsubmissionsfromdate": 0,
                    "cutoffdate": 0,
                    "intro": "",
                },
            ],
        }
    ]
}


def _fetcher(handler) -> HttpAssignmentFetcher:
    return HttpAssignmentFetcher(
        base_url=BASE,
        timeout_seconds=5.0,
        transport=httpx.MockTransport(handler),
    )


def _obtainer(handler) -> HttpTokenObtainer:
    return HttpTokenObtainer(
        base_url=BASE,
        timeout_seconds=5.0,
        transport=httpx.MockTransport(handler),
    )


async def test_fetch_assignments_parses_fields():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["wsfunction"] == "mod_assign_get_assignments"
        assert request.url.params["wstoken"] == "tok-1"
        return httpx.Response(200, json=ASSIGNMENTS_BODY)

    fetched = await _fetcher(handler).fetch_assignments(token="tok-1")
    assert len(fetched) == 2
    hw3 = fetched[0]
    assert hw3.moodle_course_id == 7001
    assert hw3.moodle_assignment_id == 555
    assert hw3.course_name == "資料結構"
    assert hw3.title == "HW3"
    assert hw3.due_at == datetime.fromtimestamp(1765400400, tz=UTC)
    assert hw3.cutoff_at is None  # unix 0 → None
    assert hw3.moodle_url == f"{BASE}/mod/assign/view.php?id=9001"
    assert hw3.intro_html == "<p>do it</p>"
    hw4 = fetched[1]
    assert hw4.due_at is None and hw4.allow_from_at is None


async def test_fetch_assignments_invalid_token():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "exception": "moodle_exception",
                "errorcode": "invalidtoken",
                "message": "Invalid token",
            },
        )

    with pytest.raises(MoodleTokenInvalid):
        await _fetcher(handler).fetch_assignments(token="dead")


async def test_fetch_assignments_rate_limited():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="slow down")

    with pytest.raises(MoodleRateLimited):
        await _fetcher(handler).fetch_assignments(token="tok")


async def test_fetch_assignments_network_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(MoodleUnreachable):
        await _fetcher(handler).fetch_assignments(token="tok")


async def test_obtain_token_success():
    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(
            pair.split("=", 1)
            for pair in request.read().decode().split("&")
        )
        assert body["username"] == "b11203058"
        assert body["service"] == "moodle_mobile_app"
        return httpx.Response(
            200, json={"token": "new-tok", "privatetoken": "new-priv"}
        )

    got = await _obtainer(handler).obtain_token(
        username="b11203058", password="pw"
    )
    assert got.token == "new-tok"
    assert got.private_token == "new-priv"


async def test_obtain_token_auth_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"error": "Invalid login", "errorcode": "invalidlogin"},
        )

    with pytest.raises(SsoAuthFailed):
        await _obtainer(handler).obtain_token(username="u", password="bad")


async def test_obtain_token_network_failure_is_not_auth_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout")

    with pytest.raises(SsoUnavailable):
        await _obtainer(handler).obtain_token(username="u", password="pw")


async def test_obtain_token_server_error_is_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="maintenance")

    with pytest.raises(SsoUnavailable):
        await _obtainer(handler).obtain_token(username="u", password="pw")
