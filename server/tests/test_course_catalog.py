"""The course catalog: the daily refresh against a stand-in for NTUST's course
query, built from its recorded answers (fixtures/querycourse.json), and the
/v3/course-catalog endpoint that serves what it stored. Nothing here reaches
the network."""

from __future__ import annotations

import gzip
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from server.academic_calendar.models import SemesterTerm
from server.auth.moodle import MoodleVerifyResult, StaticMoodleVerifier
from server.course_catalog import refresh
from server.course_catalog.models import CourseCatalogSnapshot
from server.course_catalog.refresh import (
    ATTEMPTS,
    PREFIXES,
    CatalogFetchError,
    course_query,
    refresh_course_catalog,
)
from server.db import build_session_factory
from server.scheduler.runtime import build_scheduler

pytestmark = pytest.mark.asyncio(loop_scope="session")

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "querycourse.json").read_text())
FIRST = datetime(2026, 10, 27, 21, 30, tzinfo=UTC)
SECOND = datetime(2026, 10, 28, 21, 30, tzinfo=UTC)
THIRD = datetime(2026, 10, 29, 21, 30, tzinfo=UTC)
# The app's CourseSearchResult, and the flags the refresh adds.
ROW_KEYS = {
    "Semester", "CourseNo", "CourseName", "CourseTeacher", "Dimension", "CreditPoint",
    "RequireOption", "AllYear", "ChooseStudent", "Restrict1", "Restrict2", "CourseTimes",
    "PracticalTimes", "ClassRoomNo", "Node", "Contents",
    "IsIntensive", "IsAlliance", "IsEnglishTaught",
}
_OPTION_PATHS = {"semestersinfo": "semestersinfo", "dimensions": "dimensions", "Colleges/": "colleges"}


class School:
    """querycourse.ntust.edu.tw as recorded. Keeps every request; `fail` maps
    (semester, language, CourseNo) to the status to answer that query with,
    and `edit` changes each course row it answers."""

    def __init__(
        self,
        fail: dict[tuple[str, str, str], int] | None = None,
        edit: Callable[[dict], dict] = lambda row: row,
    ) -> None:
        self.requests: list[httpx.Request] = []
        self.fail = fail or {}
        self.edit = edit

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/querycourse/api/")
        if request.method == "GET":
            if path == "departments":
                return httpx.Response(200, json=FIXTURE["departments"][request.url.params["collegeNo"]])
            return httpx.Response(200, json=FIXTURE[_OPTION_PATHS[path]])
        body = json.loads(request.content)
        status = self.fail.get((body["Semester"], body["Language"], body["CourseNo"]))
        if status:
            return httpx.Response(status, json={"Message": "error"})
        rows = FIXTURE["intensive"] if body.get("OnlyIntensive") else [
            r for r in FIXTURE["courses"][body["Language"]]
            if r["CourseNo"].upper().startswith(body["CourseNo"].upper())
        ]
        return httpx.Response(200, json=[self.edit(r | {"Semester": body["Semester"]}) for r in rows])

    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if r.method == "POST"]


@pytest.fixture
def no_backoff(monkeypatch) -> None:
    monkeypatch.setattr(refresh, "RETRY_BASE_SECONDS", 0.0)


async def _factory(db_session):
    """A session factory over the test database, with 1151 the current term."""
    db_session.add(SemesterTerm(code="1151", start_date=date(2026, 9, 7), end_date=date(2027, 1, 15)))
    await db_session.commit()
    return build_session_factory(db_session.bind)


async def _snapshots(factory) -> dict[tuple[str, str], CourseCatalogSnapshot]:
    async with factory() as session:
        rows = (await session.execute(select(CourseCatalogSnapshot))).scalars()
        return {(r.semester, r.language): r for r in rows}


def _document(snapshot: CourseCatalogSnapshot) -> dict:
    return json.loads(gzip.decompress(snapshot.body))


async def test_refresh_stores_the_current_term_and_the_next_in_both_languages(db_session) -> None:
    factory = await _factory(db_session)
    school = School()

    stored = await refresh_course_catalog(factory, transport=httpx.MockTransport(school), now=FIRST)

    # 1152 is listed after the current 1151; older terms are left alone.
    assert stored == ["1152", "1151"]
    snapshots = await _snapshots(factory)
    assert set(snapshots) == {(s, lang) for s in ("1152", "1151") for lang in ("zh", "en")}
    zh, en = _document(snapshots["1151", "zh"]), _document(snapshots["1151", "en"])
    assert (zh["semester"], zh["language"], zh["fetched_at"]) == ("1151", "zh", "2026-10-27T21:30:00Z")
    assert len(zh["courses"]) == len(en["courses"]) == len(FIXTURE["courses"]["zh"])
    assert all(set(row) == ROW_KEYS for row in zh["courses"] + en["courses"])
    assert {r["CourseNo"]: r["CourseName"] for r in en["courses"]}["3T1001701"] == "Calculus"

    assert [c["name"] for c in zh["colleges"]] == ["工程學院", "電資學院"]
    assert [c["name"] for c in en["colleges"]][0] == "College of Engineering"
    # The school lists CS twice under the college.
    assert [d["no"] for d in zh["colleges"][1]["departments"]] == ["CS", "EC", "EE"]
    # Dimension 0 is the site's "all" placeholder.
    assert [d["no"] for d in zh["dimensions"]] == list("ABCDEF")
    assert en["dimensions"][0] == {"no": "A", "name": "Humanities and Thinking(A)"}
    assert snapshots["1151", "zh"].etag.startswith('W/"')
    assert snapshots["1151", "zh"].etag != snapshots["1151", "en"].etag


async def test_flags_are_derived_from_the_school_rules(db_session) -> None:
    factory = await _factory(db_session)
    await refresh_course_catalog(factory, transport=httpx.MockTransport(School()), now=FIRST)
    snapshots = await _snapshots(factory)

    for language in ("zh", "en"):
        flags = {
            r["CourseNo"]: (r["IsIntensive"], r["IsAlliance"], r["IsEnglishTaught"])
            for r in _document(snapshots["1151", language])["courses"]
        }
        # Contents holds the English-taught mark in zh; en takes it from there.
        assert flags["EC1011301"] == (False, False, True)
        assert flags["CS1003302"] == (False, False, True)
        assert flags["EC1008301"] == (False, False, False)
        # Only the OnlyIntensive query tells; 3N and 3T are the alliance courses.
        assert flags["BA1523701"] == (True, False, False)
        assert flags["3N1346703"] == (True, True, False)
        assert flags["3T1001701"] == (False, True, False)
        # TU numbers are the school's own.
        assert flags["TU3482701"] == (False, False, False)


async def test_no_request_sends_the_empty_filter(db_session) -> None:
    factory = await _factory(db_session)
    school = School()
    await refresh_course_catalog(factory, transport=httpx.MockTransport(school), now=FIRST)

    bodies = school.bodies()
    assert bodies and all(b["CourseNo"] or b.get("OnlyIntensive") == 1 for b in bodies)
    # Per term: each prefix in both languages and one intensive query.
    assert len(bodies) == 2 * (2 * len(PREFIXES) + 1)
    with pytest.raises(ValueError):
        course_query("1151", "zh")


async def test_a_failed_request_keeps_that_terms_last_snapshot(db_session, no_backoff) -> None:
    factory = await _factory(db_session)
    await refresh_course_catalog(factory, transport=httpx.MockTransport(School()), now=FIRST)
    before = await _snapshots(factory)
    # New counts, so a term that is stored gets the second fetch's time.
    school = School(fail={("1151", "en", "M"): 500}, edit=lambda row: row | {"ChooseStudent": 1})

    with pytest.raises(CatalogFetchError, match="1151"):
        await refresh_course_catalog(factory, transport=httpx.MockTransport(school), now=SECOND)

    after = await _snapshots(factory)
    # 1151's zh fetch succeeded, but is not stored without its en.
    for language in ("zh", "en"):
        assert after["1151", language].fetched_at == FIRST
        assert after["1151", language].body == before["1151", language].body
        assert after["1152", language].fetched_at == SECOND
    failing = [b for b in school.bodies() if (b["Semester"], b["Language"], b["CourseNo"]) == ("1151", "en", "M")]
    assert len(failing) == ATTEMPTS
    # The failed term stops there rather than sweeping on.
    assert not any(b["Semester"] == "1151" and b.get("OnlyIntensive") for b in school.bodies())


async def test_an_unchanged_catalog_keeps_its_snapshot_and_etag(db_session) -> None:
    factory = await _factory(db_session)
    await refresh_course_catalog(factory, transport=httpx.MockTransport(School()), now=FIRST)
    before = await _snapshots(factory)

    await refresh_course_catalog(factory, transport=httpx.MockTransport(School()), now=SECOND)

    after = await _snapshots(factory)
    for key, snapshot in before.items():
        assert after[key].etag == snapshot.etag
        assert after[key].body == snapshot.body
        assert after[key].fetched_at == FIRST

    enrolment_moved = School(edit=lambda row: row | {"ChooseStudent": 1})
    await refresh_course_catalog(factory, transport=httpx.MockTransport(enrolment_moved), now=THIRD)

    changed = await _snapshots(factory)
    for key, snapshot in before.items():
        assert changed[key].etag != snapshot.etag
        assert changed[key].fetched_at == THIRD
        assert _document(changed[key])["fetched_at"] == "2026-10-29T21:30:00Z"


async def test_an_unreadable_row_fails_the_term(db_session) -> None:
    factory = await _factory(db_session)

    def handler(request: httpx.Request) -> httpx.Response:
        response = School()(request)
        if request.method == "POST" and json.loads(request.content)["CourseNo"] == "E":
            return httpx.Response(200, json=[r | {"CreditPoint": None} for r in response.json()])
        return response

    with pytest.raises(CatalogFetchError):
        await refresh_course_catalog(factory, transport=httpx.MockTransport(handler), now=FIRST)
    assert await _snapshots(factory) == {}


async def test_the_job_runs_daily_at_the_set_taipei_time(test_settings) -> None:
    settings = test_settings.model_copy(update={"course_catalog_refresh_time": "05:30"})
    job = build_scheduler(object(), object(), settings, llm=object()).get_job("course_catalog_refresh")
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert (fields["hour"], fields["minute"], str(job.trigger.timezone)) == ("5", "30", "Asia/Taipei")
    off = build_scheduler(object(), object(), test_settings, llm=object())
    assert off.get_job("course_catalog_refresh") is None


_LOGIN_BODY = {
    "student_id": "B11015000",
    "password": "pw",
    "moodle_token": "tok",
    "device_info": {"client_device_id": "dev-catalog", "platform": "ios"},
}


async def _bearer(client) -> dict:
    client.app.state.moodle_verifier = StaticMoodleVerifier(
        MoodleVerifyResult(ok=True, username="b11015000")
    )
    login = await client.post("/v3/auth/login", json=_LOGIN_BODY)
    assert login.status_code == 200
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def _seeded(client) -> dict:
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        session.add(SemesterTerm(code="1151", start_date=date(2026, 9, 7), end_date=date(2027, 1, 15)))
        await session.commit()
    await refresh_course_catalog(factory, transport=httpx.MockTransport(School()), now=FIRST)
    return await _bearer(client)


async def test_the_endpoint_needs_a_session(client) -> None:
    assert (await client.get("/v3/course-catalog/1151")).status_code == 401


async def test_a_term_not_fetched_answers_404(client) -> None:
    headers = await _seeded(client)
    response = await client.get("/v3/course-catalog/1142", headers=headers)
    assert response.status_code == 404
    assert response.json()["detail"] == "course_catalog_not_found"


async def test_serves_the_snapshot_gzipped_and_answers_304_to_its_etag(client) -> None:
    headers = await _seeded(client)

    response = await client.get("/v3/course-catalog/1151?language=en", headers=headers)
    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"
    assert response.headers["content-type"] == "application/json"
    assert response.json()["language"] == "en"
    assert response.json()["fetched_at"] == "2026-10-27T21:30:00Z"
    etag = response.headers["etag"]

    for tag in (etag, etag.removeprefix("W/"), f'"other", {etag}'):
        again = await client.get(
            "/v3/course-catalog/1151?language=en", headers={**headers, "If-None-Match": tag}
        )
        assert again.status_code == 304
        assert again.content == b""
        assert again.headers["etag"] == etag

    zh = await client.get("/v3/course-catalog/1151", headers={**headers, "If-None-Match": etag})
    assert zh.status_code == 200
    assert zh.json()["language"] == "zh"


async def test_a_client_that_takes_no_gzip_gets_plain_json(client) -> None:
    headers = await _seeded(client)
    response = await client.get(
        "/v3/course-catalog/1151", headers={**headers, "Accept-Encoding": "identity"}
    )
    assert response.status_code == 200
    assert "content-encoding" not in response.headers
    assert response.json()["semester"] == "1151"
