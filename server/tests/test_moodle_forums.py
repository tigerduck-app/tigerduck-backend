"""The course announcement poll's news forum calls, against a fake Moodle
(httpx.MockTransport). Every announcement here is invented."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from server.syncjobs.moodle_client import (
    HttpForumFetcher,
    MoodleRateLimited,
    MoodleRefused,
    MoodleTokenInvalid,
    MoodleUnavailable,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

BASE = "https://moodle.example.test"
TOKEN = "secret-token-0123"
_CREATED = 1_763_000_000


def _discussion(n: int, **fields) -> dict:
    """An entry as mod_forum_get_forum_discussions answers it, author fields
    included."""
    return {
        "id": 900 + n,
        "name": f"Notice {n}",
        "groupid": -1,
        "timemodified": _CREATED + n,
        "usermodified": 5,
        "timestart": 0,
        "timeend": 0,
        "discussion": 100 + n,
        "parent": 0,
        "userid": 5,
        "created": _CREATED + n,
        "modified": _CREATED + n,
        "subject": f"Notice {n}",
        "message": f"<p>Invented notice {n}</p>",
        "attachment": "",
        "userfullname": "Teacher Name",
        "usermodifiedfullname": "Teacher Name",
        "userpictureurl": "https://moodle.example.test/pic.png",
        "numreplies": 0,
        "pinned": False,
        "locked": False,
        "starred": False,
        **fields,
    }


def _fetcher(handler) -> HttpForumFetcher:
    return HttpForumFetcher(BASE, 5.0, transport=httpx.MockTransport(handler))


async def test_news_forums_come_by_course_from_one_request():
    asked: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        return httpx.Response(
            200,
            json=[
                {"id": 31, "course": 7001, "type": "general", "name": "Q&A"},
                {"id": 30, "course": 7001, "type": "news", "name": "Announcements"},
                {"id": 40, "course": 7002, "type": "news", "name": "Announcements"},
            ],
        )

    fetcher = _fetcher(handler)
    found = await fetcher.fetch_news_forums(token=TOKEN, course_ids=[7001, 7002, 7003])

    assert found == {7001: 30, 7002: 40}
    [request] = asked
    assert request.url.params["wsfunction"] == "mod_forum_get_forums_by_courses"
    assert request.url.params.get_list("courseids[0]") == ["7001"]
    assert request.url.params["courseids[2]"] == "7003"


async def test_discussions_are_read_one_explicit_page_at_a_time():
    asked: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        return httpx.Response(200, json={"discussions": [_discussion(1)], "warnings": []})

    [only] = await _fetcher(handler).fetch_discussions(token=TOKEN, forum_id=30)

    [request] = asked
    params = request.url.params
    assert params["wsfunction"] == "mod_forum_get_forum_discussions"
    assert (params["forumid"], params["page"], params["perpage"]) == ("30", "0", "20")
    assert "groupid" not in params
    assert (only.discussion_id, only.post_id) == (101, 901)
    assert only.subject == "Notice 1"
    assert only.message == "<p>Invented notice 1</p>"
    assert only.created_at == datetime.fromtimestamp(_CREATED + 1, tz=UTC)
    assert not hasattr(only, "userfullname")


async def test_group_and_visibility_start_are_read():
    starts = _CREATED + 3600

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "discussions": [
                    _discussion(1, groupid=12, timestart=starts, attachment="1"),
                    _discussion(2),
                ]
            },
        )

    grouped, everyone = await _fetcher(handler).fetch_discussions(token=TOKEN, forum_id=30)

    assert (grouped.group_id, grouped.visible_from, grouped.has_attachment) == (
        12,
        datetime.fromtimestamp(starts, tz=UTC),
        True,
    )
    assert (everyone.group_id, everyone.visible_from, everyone.has_attachment) == (
        -1,
        None,
        False,
    )


async def test_a_page_of_pinned_and_starred_discussions_still_shows_a_new_post():
    pages: dict[str, list[dict]] = {
        "0": [_discussion(n, pinned=n % 2 == 0, starred=n % 2 == 1) for n in range(20)],
        "1": [_discussion(50), _discussion(51)],
    }
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.params["page"])
        return httpx.Response(200, json={"discussions": pages[request.url.params["page"]]})

    found = await _fetcher(handler).fetch_discussions(token=TOKEN, forum_id=30)

    assert asked == ["0", "1"]
    assert 150 in {d.discussion_id for d in found}


async def test_a_pinned_forum_with_a_new_post_on_its_page_costs_one_request():
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.params["page"])
        pinned = [_discussion(n, pinned=True) for n in range(19)]
        return httpx.Response(200, json={"discussions": [*pinned, _discussion(50)]})

    found = await _fetcher(handler).fetch_discussions(token=TOKEN, forum_id=30)

    assert asked == ["0"]
    assert found[-1].discussion_id == 150


async def test_a_malformed_entry_is_skipped():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "discussions": [
                    _discussion(1, created=0),
                    _discussion(2, id="x"),
                    _discussion(3),
                ]
            },
        )

    found = await _fetcher(handler).fetch_discussions(token=TOKEN, forum_id=30)

    assert [d.discussion_id for d in found] == [103]


def _moodle_error(code: str) -> httpx.Response:
    return httpx.Response(200, json={"exception": "moodle_exception", "errorcode": code})


@pytest.mark.parametrize(
    ("response", "error", "code"),
    [
        (_moodle_error("invalidtoken"), MoodleTokenInvalid, "invalidtoken"),
        (_moodle_error("requireloginerror"), MoodleRefused, "requireloginerror"),
        (httpx.Response(200, json=[]), MoodleRefused, "unexpected_shape"),
        (httpx.Response(429), MoodleRateLimited, "http_429"),
        (httpx.Response(503), MoodleUnavailable, "http_503"),
        (httpx.Response(200, text="<html>"), MoodleUnavailable, "invalid_json"),
    ],
)
async def test_errors_carry_a_fixed_code(response, error, code):
    fetcher = _fetcher(lambda request: response)

    with pytest.raises(error) as caught:
        await fetcher.fetch_discussions(token=TOKEN, forum_id=30)

    assert caught.value.args == (code,)


@pytest.mark.parametrize("call", ["fetch_discussions", "fetch_news_forums"])
async def test_a_failed_request_keeps_the_token_out_of_the_error(call):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    fetcher = _fetcher(handler)
    args = {"forum_id": 30} if call == "fetch_discussions" else {"course_ids": [7001]}

    with pytest.raises(MoodleUnavailable) as caught:
        await getattr(fetcher, call)(token=TOKEN, **args)

    error = caught.value
    assert error.args == ("ConnectError",)
    assert error.__cause__ is None and error.__context__ is None
    assert TOKEN not in repr(error)


async def test_a_nul_character_postgres_refuses_is_dropped():
    def handler(request: httpx.Request) -> httpx.Response:
        nul = _discussion(1, subject="Notice\u00001", message="<p>a\u0000b</p>")
        return httpx.Response(200, json={"discussions": [nul]})

    [found] = await _fetcher(handler).fetch_discussions(token=TOKEN, forum_id=30)

    assert (found.subject, found.message) == ("Notice1", "<p>ab</p>")
