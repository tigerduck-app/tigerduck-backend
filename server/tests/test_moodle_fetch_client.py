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


async def test_fetch_assignments_asks_only_for_courses_the_user_can_submit_to():
    """A course the user is a TA in has assignments they grade, not submit;
    with the capability filter Moodle leaves that course out."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=ASSIGNMENTS_BODY)

    await _fetcher(handler).fetch_assignments(token="tok-1")
    [request] = seen
    assert request.url.params["capabilities[0]"] == "mod/assign:submit"


COURSE_WARNING = {
    "item": "course",
    "itemid": 7002,
    "warningcode": "1",
    "message": "No access rights in course context",
}


def _answering(body: dict) -> HttpAssignmentFetcher:
    return _fetcher(lambda request: httpx.Response(200, json=body))


@pytest.mark.parametrize(
    "body",
    [{}, {"warnings": [COURSE_WARNING]}, {"courses": None}, {"courses": {"id": 7001}}],
    ids=["empty", "warnings-only", "null-courses", "courses-not-a-list"],
)
async def test_fetch_assignments_refuses_an_answer_without_a_course_list(body):
    """Read as no assignments, such an answer soft-deleted every assignment
    stored for the user, and the next scan cancelled their reminders."""
    with pytest.raises(MoodleUnreachable, match="^unexpected_shape$"):
        await _answering(body).fetch_assignments(token="tok")


async def test_fetch_assignments_refuses_an_answer_that_lost_every_course():
    """Moodle names each course it refused in a warning. With no course
    left, the answer says nothing about the user's assignments."""
    body = {"courses": [], "warnings": [COURSE_WARNING]}
    with pytest.raises(MoodleUnreachable, match="^no_course_access$"):
        await _answering(body).fetch_assignments(token="tok")


@pytest.mark.parametrize(
    "body", [{"courses": [], "warnings": []}, {"courses": []}], ids=["warnings", "none"]
)
async def test_fetch_assignments_takes_an_empty_answer_without_course_warnings(body):
    """The capability filter leaves out a TA's course without a warning, so
    a TA-only account gets this answer, and its rows must still go."""
    assert await _answering(body).fetch_assignments(token="tok") == []


async def test_fetch_assignments_keeps_the_courses_returned_beside_a_course_warning():
    body = {**ASSIGNMENTS_BODY, "warnings": [COURSE_WARNING]}
    fetched = await _answering(body).fetch_assignments(token="tok")
    assert [a.moodle_assignment_id for a in fetched] == [555, 556]


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
