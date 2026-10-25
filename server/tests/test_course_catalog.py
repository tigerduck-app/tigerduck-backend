"""The course catalog: the refresh against a stand-in for NTUST's course
query, built from its recorded answers (fixtures/querycourse.json), and the
/v3/course-catalog endpoint that serves what it stored. Nothing here reaches
the network."""

from __future__ import annotations

import gzip
import json
import asyncio
import functools
import itertools
import string
import time
from collections.abc import Callable, Collection
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from prometheus_client import REGISTRY
from sqlalchemy import event, select

from server.academic_calendar.models import SemesterTerm
from server.auth.moodle import MoodleVerifyResult, StaticMoodleVerifier
from server.course_catalog import refresh
from server.course_catalog.models import CourseCatalogEnglish, CourseCatalogSnapshot
from server.course_catalog.refresh import (
    ATTEMPTS,
    LANES,
    PREFIXES,
    CatalogFetchError,
    course_query,
    english_due,
    refresh_course_catalog,
)
from server.db import build_session_factory
from server.scheduler import runtime
from server.scheduler.runtime import build_scheduler

pytestmark = pytest.mark.asyncio(loop_scope="session")

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "querycourse.json").read_text())
FIRST = datetime(2026, 10, 27, 21, 30, tzinfo=UTC)
SECOND = datetime(2026, 10, 28, 21, 30, tzinfo=UTC)
THIRD = datetime(2026, 10, 29, 21, 30, tzinfo=UTC)
# FIRST is 05:30 in Taipei; two hours on is the same school day.
LATER = datetime(2026, 10, 27, 23, 30, tzinfo=UTC)
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
    `edit` changes each course row it answers, an `unpublished` term is
    listed with no course yet, and a `short` CourseNo ("intensive" for the
    OnlyIntensive query) is answered with no row, as in a hiccup."""

    def __init__(
        self,
        fail: dict[tuple[str, str, str], int] | None = None,
        edit: Callable[[dict], dict] = lambda row: row,
        unpublished: Collection[str] = (),
        short: Collection[str] = (),
    ) -> None:
        self.requests: list[httpx.Request] = []
        self.fail = fail or {}
        self.edit = edit
        self.unpublished = unpublished
        self.short = short

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
        query = "intensive" if body.get("OnlyIntensive") else body["CourseNo"]
        if body["Semester"] in self.unpublished or query in self.short:
            return httpx.Response(200, json=[])
        rows = FIXTURE["intensive"] if body.get("OnlyIntensive") else [
            r for r in FIXTURE["courses"][body["Language"]]
            if r["CourseNo"].upper().startswith(body["CourseNo"].upper())
        ]
        return httpx.Response(200, json=[self.edit(r | {"Semester": body["Semester"]}) for r in rows])

    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if r.method == "POST"]


def _over_a_network(school: School) -> httpx.MockTransport:
    """`school` answering after a pause, so that the lanes take turns."""

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.001)
        return school(request)

    return httpx.MockTransport(handler)


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


async def _english(factory) -> dict[str, CourseCatalogEnglish]:
    async with factory() as session:
        return {r.semester: r for r in (await session.execute(select(CourseCatalogEnglish))).scalars()}


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


async def test_en_is_swept_once_a_day_after_five_in_taipei() -> None:
    def taipei(day: int, hour: int, minute: int = 0) -> datetime:
        return datetime(2026, 10, day, hour, minute, tzinfo=ZoneInfo("Asia/Taipei"))

    assert english_due(None, taipei(28, 4))
    assert not english_due(taipei(28, 4), taipei(28, 4, 30))
    assert english_due(taipei(28, 4), taipei(28, 5, 10))
    assert not english_due(taipei(28, 5, 10), taipei(28, 23))
    assert not english_due(taipei(28, 23), taipei(29, 4, 59))
    assert english_due(taipei(28, 23), taipei(29, 5))


async def test_a_run_between_en_sweeps_builds_en_from_zh(db_session) -> None:
    factory = await _factory(db_session)
    await refresh_course_catalog(factory, transport=httpx.MockTransport(School()), now=FIRST)

    def moved(row: dict) -> dict:
        # New counts everywhere, and one course at a new time the en text lacks.
        row = row | {"ChooseStudent": 7}
        return row | {"Node": "M1"} if row["CourseNo"] == "EC1011301" else row

    later = School(edit=moved)
    await refresh_course_catalog(factory, transport=httpx.MockTransport(later), now=LATER)

    assert {b["Language"] for b in later.bodies()} == {"zh"}
    en = {r["CourseNo"]: r for r in _document((await _snapshots(factory))["1151", "en"])["courses"]}
    assert {r["ChooseStudent"] for r in en.values()} == {7}
    assert en["3T1001701"]["CourseName"] == "Calculus"
    assert en["EC1011301"]["CourseName"] == "計算機程式與應用"
    assert en["EC1011301"]["IsEnglishTaught"]
    assert (await _english(factory))["1151"].fetched_at == FIRST

    next_day = School(edit=moved)
    await refresh_course_catalog(factory, transport=httpx.MockTransport(next_day), now=SECOND)

    assert {b["Language"] for b in next_day.bodies()} == {"zh", "en"}
    en = {r["CourseNo"]: r for r in _document((await _snapshots(factory))["1151", "en"])["courses"]}
    assert en["EC1011301"]["CourseName"].startswith("Computer Programming")


async def test_a_short_en_sweep_keeps_the_stored_text_and_stays_due(db_session) -> None:
    factory = await _factory(db_session)
    await refresh_course_catalog(factory, transport=httpx.MockTransport(School()), now=FIRST)
    school = School(edit=lambda row: row | {"ChooseStudent": 1})

    def en_hiccup(request: httpx.Request) -> httpx.Response:
        # EC answers en with nothing: 7 of the 10 course numbers are left.
        if request.method == "POST" and json.loads(request.content)["Language"] == "en" and (
            json.loads(request.content)["CourseNo"] == "EC"
        ):
            return httpx.Response(200, json=[])
        return school(request)

    with pytest.raises(CatalogFetchError, match="en text of 1152, 1151"):
        await refresh_course_catalog(factory, transport=httpx.MockTransport(en_hiccup), now=SECOND)

    after = await _snapshots(factory)
    assert after["1151", "zh"].fetched_at == SECOND
    en = {r["CourseNo"]: r for r in _document(after["1151", "en"])["courses"]}
    assert en["EC1011301"]["CourseName"] == "Computer Programming and Applications"
    stored = (await _english(factory))["1151"]
    assert stored.fetched_at == FIRST
    assert english_due(stored.fetched_at, SECOND)


async def test_a_matched_en_row_keeps_its_stored_text_until_the_next_en_sweep(db_session) -> None:
    factory = await _factory(db_session)
    await refresh_course_catalog(factory, transport=httpx.MockTransport(School()), now=FIRST)

    def renamed(row: dict) -> dict:
        if row["CourseNo"] != "3T1001701":
            return row
        return row | {"CourseName": "Renamed", "CourseTeacher": "Someone Else"}

    await refresh_course_catalog(factory, transport=httpx.MockTransport(School(edit=renamed)), now=LATER)
    snapshots = await _snapshots(factory)
    zh = {r["CourseNo"]: r for r in _document(snapshots["1151", "zh"])["courses"]}
    en = {r["CourseNo"]: r for r in _document(snapshots["1151", "en"])["courses"]}
    assert zh["3T1001701"]["CourseName"] == "Renamed"
    assert (en["3T1001701"]["CourseName"], en["3T1001701"]["CourseTeacher"]) == ("Calculus", "Teacher 3T")

    await refresh_course_catalog(factory, transport=httpx.MockTransport(School(edit=renamed)), now=SECOND)
    en = {r["CourseNo"]: r for r in _document((await _snapshots(factory))["1151", "en"])["courses"]}
    assert (en["3T1001701"]["CourseName"], en["3T1001701"]["CourseTeacher"]) == ("Renamed", "Someone Else")


async def test_the_prefixes_cover_every_course_number_once() -> None:
    """Course numbers are upper-case letters and digits, and no prefix is
    longer than three characters."""
    prefixes = set(PREFIXES)
    assert len(prefixes) == len(PREFIXES)
    for start in map("".join, itertools.product(string.digits + string.ascii_uppercase, repeat=3)):
        assert sum(start[:n] in prefixes for n in (1, 2, 3)) == 1, start
    # The prefixes with the most rows are not queried whole.
    assert not prefixes & {"3", "3N", "3T", "C", "E", "M", "T"}


async def test_a_sweep_keeps_at_most_three_requests_in_flight(db_session) -> None:
    factory = await _factory(db_session)
    school = School()
    in_flight = peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.001)
        in_flight -= 1
        return school(request)

    await refresh_course_catalog(factory, transport=httpx.MockTransport(handler), now=FIRST)
    assert peak == LANES == 3


async def test_a_failed_zh_request_keeps_that_terms_last_snapshot(db_session, no_backoff) -> None:
    factory = await _factory(db_session)
    await refresh_course_catalog(factory, transport=httpx.MockTransport(School()), now=FIRST)
    before = await _snapshots(factory)
    # New counts, so a term that is stored gets the second fetch's time.
    school = School(fail={("1151", "zh", "0"): 500}, edit=lambda row: row | {"ChooseStudent": 1})

    with pytest.raises(CatalogFetchError, match="1151"):
        await refresh_course_catalog(factory, transport=_over_a_network(school), now=SECOND)

    after = await _snapshots(factory)
    for language in ("zh", "en"):
        assert after["1151", language].fetched_at == FIRST
        assert after["1151", language].body == before["1151", language].body
        assert after["1152", language].fetched_at == SECOND
    failing = [b for b in school.bodies() if (b["Semester"], b["Language"], b["CourseNo"]) == ("1151", "zh", "0")]
    assert len(failing) == ATTEMPTS
    # The failed sweep stops there rather than sweeping on, and en is not swept.
    sent = [b for b in school.bodies() if b["Semester"] == "1151"]
    assert len(sent) < 20 and {b["Language"] for b in sent} == {"zh"}
    assert (await _english(factory))["1151"].fetched_at == FIRST


async def test_a_failed_en_sweep_still_stores_zh_and_stays_due(db_session, no_backoff) -> None:
    factory = await _factory(db_session)
    await refresh_course_catalog(factory, transport=httpx.MockTransport(School()), now=FIRST)
    school = School(fail={("1151", "en", "0"): 500}, edit=lambda row: row | {"ChooseStudent": 1})

    with pytest.raises(CatalogFetchError, match="en text of 1151"):
        await refresh_course_catalog(factory, transport=_over_a_network(school), now=SECOND)

    after = await _snapshots(factory)
    assert after["1151", "zh"].fetched_at == SECOND
    # en is built from the stored text, as between en sweeps.
    en = {r["CourseNo"]: r for r in _document(after["1151", "en"])["courses"]}
    assert after["1151", "en"].fetched_at == SECOND
    assert {r["ChooseStudent"] for r in en.values()} == {1}
    assert en["3T1001701"]["CourseName"] == "Calculus"
    assert (await _english(factory))["1151"].fetched_at == FIRST

    retry = School(edit=lambda row: row | {"ChooseStudent": 1})
    await refresh_course_catalog(factory, transport=httpx.MockTransport(retry), now=SECOND + timedelta(hours=2))
    assert any(b["Semester"] == "1151" and b["Language"] == "en" for b in retry.bodies())
    assert (await _english(factory))["1151"].fetched_at == SECOND + timedelta(hours=2)


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


async def test_a_later_term_without_courses_yet_is_skipped(db_session) -> None:
    factory = await _factory(db_session)
    school = School(unpublished={"1152"})

    stored = await refresh_course_catalog(factory, transport=httpx.MockTransport(school), now=FIRST)

    assert stored == ["1151"]
    assert set(await _snapshots(factory)) == {("1151", "zh"), ("1151", "en")}
    # An empty zh sweep ends the term before its en sweep.
    assert not any(b["Semester"] == "1152" and b["Language"] == "en" for b in school.bodies())


async def test_a_current_term_without_courses_fails(db_session) -> None:
    factory = await _factory(db_session)
    school = School(unpublished={"1151"})

    with pytest.raises(CatalogFetchError, match="1151"):
        await refresh_course_catalog(factory, transport=httpx.MockTransport(school), now=FIRST)
    assert set(await _snapshots(factory)) == {("1152", "zh"), ("1152", "en")}


def _runs(outcome: str) -> float:
    return REGISTRY.get_sample_value(
        "tigerduck_scheduler_job_runs_total",
        {"job_id": "course_catalog_refresh", "outcome": outcome},
    )


async def test_a_run_that_skips_a_term_counts_as_a_success(
    db_session, test_settings, monkeypatch
) -> None:
    """The stale-catalog alert reads the job's last success."""
    factory = await _factory(db_session)
    school = School(unpublished={"1152"})
    monkeypatch.setattr(
        runtime,
        "refresh_course_catalog",
        functools.partial(refresh_course_catalog, transport=httpx.MockTransport(school), now=FIRST),
    )
    settings = test_settings.model_copy(update={"course_catalog_refresh_interval_minutes": 120})
    scheduler = build_scheduler(factory, object(), settings, llm=object())
    for job in scheduler.get_jobs():
        if job.id != "course_catalog_refresh":
            job.remove()
    success, error, started = _runs("success"), _runs("error"), time.time()

    scheduler.start()
    try:
        scheduler.get_job("course_catalog_refresh").modify(next_run_time=datetime.now(UTC))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and (_runs("success"), _runs("error")) == (success, error):
            await asyncio.sleep(0.01)
    finally:
        scheduler.shutdown(wait=False)

    assert (_runs("success"), _runs("error")) == (success + 1, error)
    assert REGISTRY.get_sample_value(
        "tigerduck_scheduler_job_last_success_timestamp_seconds",
        {"job_id": "course_catalog_refresh"},
    ) >= started


@pytest.mark.parametrize(
    "short",
    [
        # EC holds 3 of the term's 11 courses, a 27% drop.
        {"EC"},
        # Every intensive course would lose its flag.
        {"intensive"},
    ],
)
async def test_a_short_answer_keeps_the_stored_snapshot(db_session, short) -> None:
    factory = await _factory(db_session)
    await refresh_course_catalog(factory, transport=httpx.MockTransport(School()), now=FIRST)
    before = await _snapshots(factory)
    hiccup = School(short=short, edit=lambda row: row | {"ChooseStudent": 1})

    with pytest.raises(CatalogFetchError, match="1152, 1151"):
        await refresh_course_catalog(factory, transport=httpx.MockTransport(hiccup), now=LATER)

    after = await _snapshots(factory)
    for key, snapshot in before.items():
        assert (after[key].body, after[key].fetched_at) == (snapshot.body, FIRST)


async def test_a_drop_of_ten_percent_or_less_is_stored(db_session) -> None:
    factory = await _factory(db_session)
    await refresh_course_catalog(factory, transport=httpx.MockTransport(School()), now=FIRST)

    # TU holds 1 of the 11 courses: 9%, a course the school may have cancelled.
    await refresh_course_catalog(factory, transport=httpx.MockTransport(School(short={"TU"})), now=LATER)

    zh = _document((await _snapshots(factory))["1151", "zh"])
    assert len(zh["courses"]) == len(FIXTURE["courses"]["zh"]) - 1


async def test_a_first_snapshot_is_stored_whatever_its_size(db_session) -> None:
    factory = await _factory(db_session)
    school = School(short={"EC", "intensive"})

    await refresh_course_catalog(factory, transport=httpx.MockTransport(school), now=FIRST)

    zh = _document((await _snapshots(factory))["1151", "zh"])
    assert len(zh["courses"]) == len(FIXTURE["courses"]["zh"]) - 3


@pytest.mark.parametrize("listed", [[], [{"Semester": "95 1"}]])
async def test_a_semester_list_without_a_term_fails_the_run(db_session, listed) -> None:
    factory = await _factory(db_session)
    school = School()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/semestersinfo"):
            return httpx.Response(200, json=listed)
        return school(request)

    with pytest.raises(CatalogFetchError, match="semestersinfo"):
        await refresh_course_catalog(factory, transport=httpx.MockTransport(handler), now=FIRST)
    assert not school.bodies()


async def test_an_unreadable_row_fails_the_term(db_session) -> None:
    factory = await _factory(db_session)

    def handler(request: httpx.Request) -> httpx.Response:
        response = School()(request)
        if request.method == "POST" and json.loads(request.content)["CourseNo"] == "EC":
            return httpx.Response(200, json=[r | {"CreditPoint": None} for r in response.json()])
        return response

    with pytest.raises(CatalogFetchError):
        await refresh_course_catalog(factory, transport=httpx.MockTransport(handler), now=FIRST)
    assert await _snapshots(factory) == {}


async def test_the_job_runs_every_two_hours_from_start(test_settings) -> None:
    """No run at start: a deploy overlap or a crash loop must not start extra
    sweeps against the school."""
    settings = test_settings.model_copy(update={"course_catalog_refresh_interval_minutes": 120})
    scheduler = build_scheduler(object(), object(), settings, llm=object())
    scheduler.start(paused=True)
    try:
        started = datetime.now(UTC)
        job = scheduler.get_job("course_catalog_refresh")
        assert job.trigger.interval == timedelta(hours=2)
        assert job.next_run_time - started > timedelta(hours=1, minutes=59)
    finally:
        scheduler.shutdown(wait=False)
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


async def test_a_304_does_not_read_the_body(client) -> None:
    headers = await _seeded(client)
    etag = (await client.get("/v3/course-catalog/1151", headers=headers)).headers["etag"]
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:
        statements.append(statement)

    engine = client.app.state.engine.sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        again = await client.get("/v3/course-catalog/1151", headers={**headers, "If-None-Match": etag})
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert again.status_code == 304
    catalog = [s for s in statements if "course_catalog_snapshots" in s]
    assert catalog and not any("course_catalog_snapshots.body" in s for s in catalog)


async def test_a_client_that_takes_no_gzip_gets_plain_json(client) -> None:
    headers = await _seeded(client)
    response = await client.get(
        "/v3/course-catalog/1151", headers={**headers, "Accept-Encoding": "identity"}
    )
    assert response.status_code == 200
    assert "content-encoding" not in response.headers
    assert response.json()["semester"] == "1151"


@pytest.mark.parametrize(
    ("accept_encoding", "gzipped"),
    [
        ("gzip, deflate, br", True),
        ("br;q=1.0, GZIP;q=0.5", True),
        ("*", True),
        ("gzip;q=0", False),
        ("gzip; q=0.000, br", False),
        ("x-gzip", False),
        ("gzip;q=0, *", False),
        ("identity", False),
    ],
)
async def test_gzip_goes_only_to_a_client_that_accepts_it(client, accept_encoding, gzipped) -> None:
    headers = await _seeded(client)
    response = await client.get(
        "/v3/course-catalog/1151", headers={**headers, "Accept-Encoding": accept_encoding}
    )
    assert response.status_code == 200
    assert (response.headers.get("content-encoding") == "gzip") is gzipped
    assert response.json()["semester"] == "1151"
