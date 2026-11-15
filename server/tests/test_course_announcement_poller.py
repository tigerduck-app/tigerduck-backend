"""The course announcement poll, against a fake Moodle and classifier.
Every announcement here is invented."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, date, datetime, timedelta

import pytest
import sqlalchemy as sa
import pytest_asyncio
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from structlog.testing import capture_logs

from server.academic_calendar.models import SemesterTerm
from server.auth.crypto import CredentialCipher
from server.auth.models import ExternalAccount, User, UserDevice
from server.course_announcements.history import import_history, read_export
from server.course_announcements.models import (
    CourseAnnouncement,
    CourseAnnouncementCourse,
    CourseAnnouncementExtraction,
)
from server.course_announcements import poller as poller_module
from server.course_announcements.poller import (
    CourseAnnouncementPoller,
    PollTickFailed,
    _in_term,
    build_poller,
    poll_interval,
    run_poll_tick,
)
from server.db import build_session_factory
from server.tests.conftest import RecordingClassifier
from server.scheduler.runtime import build_scheduler
from server.syncjobs.assignments import moodle_semester
from server.syncjobs.availability import SUSPENDED_UNTIL_KEY
from server.syncjobs.budget import RequestBudget
from server.syncjobs.executor import SyncWorker
from server.syncjobs.models import UserMoodleCourse
from server.syncjobs.moodle_client import (
    FetchedDiscussion,
    MoodleRateLimited,
    MoodleRefused,
    MoodleTokenInvalid,
    MoodleUnavailable,
)
from server.system_settings import SystemSetting

pytestmark = pytest.mark.asyncio(loop_scope="session")

# 10:00 in Taipei, a Monday of term 1151.
NOW = datetime(2026, 11, 16, 2, 0, tzinfo=UTC)


class FakeMoodle:
    """Each course's news forum and each forum's discussions; `errors` maps a
    token to what every call with it raises. Records every call."""

    def __init__(self) -> None:
        self.forums: dict[int, int] = {}
        self.discussions: dict[int, list[FetchedDiscussion]] = {}
        self.errors: dict[str, Exception] = {}
        self.calls: list[tuple] = []

    async def fetch_news_forums(self, *, token, course_ids):
        self.calls.append(("forums", token, tuple(course_ids)))
        if token in self.errors:
            raise self.errors[token]
        return {c: self.forums[c] for c in course_ids if c in self.forums}

    async def fetch_discussions(self, *, token, forum_id):
        self.calls.append(("discussions", token, forum_id))
        if token in self.errors:
            raise self.errors[token]
        return self.discussions.get(forum_id, [])

    def polled(self) -> list[tuple]:
        return [call for call in self.calls if call[0] == "discussions"]


class _Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


class _Now:
    """The poll's wall clock: `at` until a test moves it."""

    at = NOW

    def __call__(self) -> datetime:
        return self.at


async def _tick(poller: CourseAnnouncementPoller, at: datetime) -> int:
    poller.clock.at = at
    return await run_poll_tick(poller)


def _discussion(
    n: int, posted: datetime, subject: str = "Class cancelled"
) -> FetchedDiscussion:
    return FetchedDiscussion(
        discussion_id=100 + n,
        post_id=900 + n,
        subject=subject,
        message=f"<p>Invented notice {n}</p>",
        created_at=posted,
        modified_at=posted,
        group_id=-1,
        visible_from=None,
        has_attachment=False,
        pinned=False,
        starred=False,
    )


@pytest.fixture
def moodle() -> FakeMoodle:
    return FakeMoodle()


@pytest.fixture
def classifier() -> RecordingClassifier:
    return RecordingClassifier({"Class cancelled": {"class_cancel": 0.98}})


@pytest.fixture
def poller(prepared_engine, test_settings, moodle, classifier) -> CourseAnnouncementPoller:
    settings = test_settings.model_copy(update={"course_announcement_requests_per_minute": 20})
    clock = _Clock()
    return CourseAnnouncementPoller(
        session_factory=build_session_factory(prepared_engine),
        settings=settings,
        cipher=CredentialCipher.from_settings(test_settings),
        fetcher=moodle,
        classifier=classifier,
        budget=RequestBudget(20, observe=lambda: None, clock=clock),
        sync_budget=RequestBudget(10, observe=lambda: None, clock=clock),
        clock=_Now(),
    )


@pytest_asyncio.fixture(loop_scope="session")
async def term(db_session):
    db_session.add(
        SemesterTerm(code="1151", start_date=date(2026, 9, 7), end_date=date(2027, 1, 15))
    )
    await db_session.commit()


@pytest.fixture
def holder(db_session, store_moodle_token):
    """A user enrolled in the given Moodle courses, with a token and, unless
    told otherwise, a device the sync worker serves."""

    async def _make(
        name: str,
        courses: dict[int, tuple[str, str]],
        *,
        syncing: bool = True,
        status: str = "active",
    ) -> User:
        user = User(student_id=name)
        db_session.add(user)
        await db_session.flush()
        db_session.add(
            UserDevice(
                user_id=user.id,
                client_device_id=f"{name}-phone",
                platform="ios",
                cloud_sync_enabled=syncing,
                last_seen_at=NOW - timedelta(days=1),
            )
        )
        await store_moodle_token(user.id, token=f"token-{name}", status=status)
        db_session.add_all(
            UserMoodleCourse(
                user_id=user.id,
                moodle_course_id=course_id,
                idnumber=idnumber,
                full_name=full_name,
                last_seen_at=NOW,
            )
            for course_id, (idnumber, full_name) in courses.items()
        )
        await db_session.commit()
        return user

    return _make


DS = {7001: ("1151CS2006301", "Data Structures")}


async def _state(db_session, course_id: int = 7001) -> CourseAnnouncementCourse | None:
    return await db_session.get(CourseAnnouncementCourse, course_id, populate_existing=True)


async def _set_state(db_session, course_id: int = 7001, **values) -> None:
    db_session.add(
        CourseAnnouncementCourse(moodle_course_id=course_id, term_code="1151", **values)
    )
    await db_session.commit()


# --- Interval ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("age", "interval"),
    [
        (None, timedelta(days=1)),
        (timedelta(0), timedelta(minutes=15)),
        (timedelta(days=7) - timedelta(seconds=1), timedelta(minutes=15)),
        (timedelta(days=7), timedelta(hours=3)),
        (timedelta(days=14), timedelta(hours=3)),
        (timedelta(days=14, seconds=1), timedelta(hours=12)),
    ],
    ids=["none", "new", "under-7-days", "7-days", "14-days", "past-14-days"],
)
async def test_the_interval_follows_the_latest_post(age, interval):
    assert poll_interval(None if age is None else NOW - age, NOW) == interval


async def test_a_poll_stores_the_posts_and_schedules_by_the_latest(
    db_session, poller, moodle, holder, term
):
    await holder("alice", DS)
    moodle.forums[7001] = 30
    moodle.discussions[30] = [_discussion(1, NOW - timedelta(days=2))]

    assert await _tick(poller, NOW) == 1

    state = await _state(db_session)
    assert (state.forum_id, state.latest_post_at, state.next_due_at) == (
        30,
        NOW - timedelta(days=2),
        NOW + timedelta(minutes=15),
    )
    assert state.course_codes == ["CS2006301"]
    row = await db_session.get(CourseAnnouncement, "101-901", populate_existing=True)
    assert (row.term_code, row.moodle_course_id, row.route) == ("1151", 7001, "sent")


async def test_an_active_course_is_due_15_minutes_after_its_poll(
    db_session, poller, moodle, holder, term
):
    await holder("alice", DS)
    await _set_state(
        db_session,
        forum_id=30,
        latest_post_at=NOW - timedelta(days=2),
        next_due_at=NOW - timedelta(seconds=1),
    )

    assert await _tick(poller, NOW) == 1
    assert moodle.polled() == [("discussions", "token-alice", 30)]


async def test_a_course_that_never_posted_waits_a_day(db_session, poller, moodle, holder, term):
    await holder("alice", DS)
    moodle.forums[7001] = 30

    assert await _tick(poller, NOW) == 1
    assert await _tick(poller, NOW + timedelta(hours=6)) == 0
    assert await _tick(poller, NOW + timedelta(hours=24)) == 1
    assert len(moodle.polled()) == 2


@pytest.mark.parametrize("term", ["1151", "114H"])
@pytest.mark.parametrize(
    "idnumber",
    [
        "1151CS2006301",
        "1151",
        "115",
        "",
        "114hEE1001301",
        "114HEE1001301",
        "1141CS2006301",
        "x151CS2006301",
        "115-CS2006301",
        "\uff11\uff11\uff15\uff11CS2006301",
    ],
)
async def test_the_term_filter_agrees_with_moodle_semester(db_session, idnumber, term):
    in_term = await db_session.scalar(select(_in_term(sa.literal(idnumber), term)))
    assert in_term == (moodle_semester(idnumber) == term)


# --- When Moodle may be polled ---------------------------------------------------


async def test_nothing_goes_to_moodle_in_the_maintenance_window(poller, moodle, holder, term):
    await holder("alice", DS)
    half_past_two_in_taipei = datetime(2026, 11, 15, 18, 30, tzinfo=UTC)

    assert await _tick(poller, half_past_two_in_taipei) == 0
    assert moodle.calls == []


async def test_nothing_goes_to_moodle_while_an_operator_suspends_it(
    db_session, poller, moodle, holder, term
):
    await holder("alice", DS)
    db_session.add(
        SystemSetting(
            key=SUSPENDED_UNTIL_KEY,
            value={"until": (NOW + timedelta(hours=1)).isoformat()},
        )
    )
    await db_session.commit()

    assert await _tick(poller, NOW) == 0
    assert moodle.calls == []
    assert await _tick(poller, NOW + timedelta(hours=1)) == 1


async def test_a_suspend_during_a_tick_stops_the_rest_of_it(
    db_session, poller, moodle, holder, term
):
    await holder("alice", {7001: DS[7001], 7002: ("1151CS2007301", "Algorithms")})
    for course_id in (7001, 7002):
        await _set_state(db_session, course_id, forum_id=course_id, next_due_at=NOW)
    answer = moodle.fetch_discussions

    async def suspended_meanwhile(*, token, forum_id):
        db_session.add(
            SystemSetting(
                key=SUSPENDED_UNTIL_KEY,
                value={"until": (NOW + timedelta(hours=1)).isoformat()},
            )
        )
        await db_session.commit()
        return await answer(token=token, forum_id=forum_id)

    moodle.fetch_discussions = suspended_meanwhile

    assert await _tick(poller, NOW) == 1
    assert [call[2] for call in moodle.polled()] == [7001]


def _taking(moodle: FakeMoodle, poller: CourseAnnouncementPoller, until: datetime) -> None:
    """Make Moodle's first discussion answer arrive at `until`."""
    answer = moodle.fetch_discussions

    async def slow(*, token, forum_id):
        poller.clock.at = until
        return await answer(token=token, forum_id=forum_id)

    moodle.fetch_discussions = slow


async def test_a_tick_that_runs_into_the_maintenance_window_stops_there(
    db_session, poller, moodle, holder, term
):
    await holder("alice", {7001: DS[7001], 7002: ("1151CS2007301", "Algorithms")})
    for course_id in (7001, 7002):
        await _set_state(db_session, course_id, forum_id=course_id, next_due_at=NOW)
    two_to_midnight_in_taipei = datetime(2026, 11, 16, 15, 58, tzinfo=UTC)
    _taking(moodle, poller, until=two_to_midnight_in_taipei + timedelta(minutes=2))

    assert await _tick(poller, two_to_midnight_in_taipei) == 1
    assert [call[2] for call in moodle.polled()] == [7001]


async def test_a_holder_whose_device_goes_quiet_during_a_tick_lends_no_more(
    db_session, poller, moodle, holder, term
):
    alice = await holder("alice", {7001: DS[7001], 7002: ("1151CS2007301", "Algorithms")})
    for course_id in (7001, 7002):
        await _set_state(db_session, course_id, forum_id=course_id, next_due_at=NOW)
    device = (await db_session.scalars(select(UserDevice).filter_by(user_id=alice.id))).one()
    active_for = timedelta(days=poller.settings.sync_job_device_active_days)
    device.last_seen_at = NOW - active_for + timedelta(seconds=1)
    await db_session.commit()
    _taking(moodle, poller, until=NOW + timedelta(seconds=2))

    assert await _tick(poller, NOW) == 2
    assert [call[2] for call in moodle.polled()] == [7001]
    assert (await _state(db_session, 7002)).holder_user_id is None


async def test_a_course_of_another_term_is_not_polled(poller, moodle, holder, term):
    await holder("alice", {7009: ("1142CS2006301", "Data Structures")})
    assert await _tick(poller, NOW) == 0
    assert moodle.calls == []


# --- Budget and order --------------------------------------------------------------


async def test_the_longest_due_go_first_and_the_rest_wait(
    db_session, poller, moodle, holder, term
):
    courses = {course_id: (f"1151CS{course_id}01", "Course") for course_id in (1, 2, 3, 4)}
    await holder("alice", courses)
    due = {
        1: NOW - timedelta(minutes=1),
        2: NOW - timedelta(hours=2),
        3: NOW + timedelta(minutes=5),
    }
    for course_id, next_due_at in due.items():
        await _set_state(db_session, course_id, forum_id=course_id * 10, next_due_at=next_due_at)
    moodle.forums[4] = 40
    two = poller.settings.model_copy(update={"course_announcement_requests_per_minute": 2})
    limited = dataclasses.replace(poller, settings=two)

    assert await _tick(limited, NOW) == 2
    assert [call[2] for call in moodle.polled()] == [40, 20]
    assert await _tick(limited, NOW) == 1
    assert [call[2] for call in moodle.polled()] == [40, 20, 10]


@pytest.mark.parametrize("paused", ["sync_budget", "budget"])
async def test_no_poll_while_either_budget_is_paused(poller, moodle, holder, term, paused):
    await holder("alice", DS)
    getattr(poller, paused).pause(300)

    assert await _tick(poller, NOW) == 0
    assert moodle.calls == []


async def test_a_rate_limit_pauses_both_budgets_and_ends_the_tick(
    db_session, poller, moodle, holder, term
):
    await holder("alice", {7001: DS[7001], 7002: ("1151CS2007301", "Algorithms")})
    for course_id in (7001, 7002):
        await _set_state(db_session, course_id, forum_id=course_id, next_due_at=NOW)
    moodle.errors["token-alice"] = MoodleRateLimited("http_429")

    assert await _tick(poller, NOW) == 0

    assert len(moodle.polled()) == 1
    assert poller.budget.paused and poller.sync_budget.paused
    assert (await _state(db_session)).next_due_at == NOW


async def test_moodle_failing_on_a_course_ends_the_tick_and_defers_that_course(
    db_session, poller, moodle, holder, term
):
    await holder("alice", {7001: DS[7001], 7002: ("1151CS2007301", "Algorithms")})
    for course_id in (7001, 7002):
        await _set_state(db_session, course_id, forum_id=course_id, next_due_at=NOW)
    moodle.errors["token-alice"] = MoodleUnavailable("http_503")

    assert await _tick(poller, NOW) == 0
    assert [call[2] for call in moodle.polled()] == [7001]
    assert (await _state(db_session, 7001)).next_due_at == NOW + timedelta(minutes=15)
    assert (await _state(db_session, 7002)).next_due_at == NOW
    assert not poller.budget.paused

    del moodle.errors["token-alice"]
    assert await _tick(poller, NOW + timedelta(minutes=1)) == 1
    assert [call[2] for call in moodle.polled()] == [7001, 7002]


# --- Holders -----------------------------------------------------------------------


@pytest.mark.parametrize("ineligible", ["sync_off", "credential_invalid"])
async def test_a_course_without_an_eligible_holder_is_not_polled(
    poller, moodle, holder, term, ineligible
):
    if ineligible == "sync_off":
        await holder("alice", DS, syncing=False)
    else:
        await holder("alice", DS, status="invalid")

    assert await _tick(poller, NOW) == 0
    assert moodle.calls == []


async def test_only_an_eligible_holder_lends_a_token(db_session, poller, moodle, holder, term):
    await holder("alice", DS, syncing=False)
    await holder("bob", DS, status="invalid")
    await holder("carol", DS)
    await _set_state(db_session, forum_id=30)

    await _tick(poller, NOW)

    assert moodle.polled() == [("discussions", "token-carol", 30)]


@pytest.mark.parametrize(
    "error", [MoodleTokenInvalid("invalidtoken"), MoodleRefused("requireloginerror")]
)
async def test_a_refused_holder_hands_the_course_on_and_keeps_their_credential(
    db_session, poller, moodle, holder, term, error
):
    first = await holder("alice", DS)
    second = await holder("bob", DS)
    await _set_state(db_session, forum_id=30, holder_user_id=first.id)
    moodle.forums[7001] = 30
    moodle.errors["token-alice"] = error

    assert await _tick(poller, NOW) == 1
    state = await _state(db_session)
    assert state.holder_user_id == second.id
    assert state.next_due_at == NOW + timedelta(minutes=15)
    assert state.forum_id == (30 if isinstance(error, MoodleTokenInvalid) else None)

    assert await _tick(poller, state.next_due_at) == 1
    assert [call[1] for call in moodle.polled()] == ["token-alice", "token-bob"]
    statuses = await db_session.scalars(
        select(ExternalAccount.credential_status).execution_options(populate_existing=True)
    )
    assert set(statuses) == {"active"}
    assert (await _state(db_session)).holder_user_id == second.id


async def test_a_refused_forum_is_looked_up_again_on_the_next_poll(
    db_session, poller, moodle, holder, term
):
    await holder("alice", DS)
    await _set_state(db_session, forum_id=30)
    moodle.forums[7001] = 31
    answer = moodle.fetch_discussions

    async def deleted_forum(*, token, forum_id):
        discussions = await answer(token=token, forum_id=forum_id)
        if forum_id == 30:
            raise MoodleRefused("Unable to find forum with id 30")
        return discussions

    moodle.fetch_discussions = deleted_forum

    assert await _tick(poller, NOW) == 1
    state = await _state(db_session)
    assert (state.forum_id, state.next_due_at) == (None, NOW + timedelta(days=1))

    assert await _tick(poller, state.next_due_at) == 1
    assert [call[0] for call in moodle.calls] == ["discussions", "forums", "discussions"]
    assert [call[2] for call in moodle.polled()] == [30, 31]
    assert (await _state(db_session)).forum_id == 31


# --- Forums and courses --------------------------------------------------------------


async def test_one_lookup_finds_the_forums_of_the_holders_other_courses(
    db_session, poller, moodle, holder, term
):
    await holder("alice", {7001: DS[7001], 7002: ("1151CS2007301", "Algorithms")})
    moodle.forums.update({7001: 30, 7002: 31})

    assert await _tick(poller, NOW) == 2

    lookups = [call for call in moodle.calls if call[0] == "forums"]
    assert lookups == [("forums", "token-alice", (7001, 7002))]
    assert sorted(call[2] for call in moodle.polled()) == [30, 31]


async def test_the_forums_one_lookup_finds_are_stored_in_course_order(
    db_session, prepared_engine, poller, moodle, holder, term
):
    others = {7004: "1151CS2009301", 7003: "1151CS2008301", 7002: "1151CS2007301"}
    await holder("alice", {7001: DS[7001]} | {c: (i, "Elective") for c, i in others.items()})
    moodle.forums.update({7001: 30, 7002: 31, 7003: 32, 7004: 33})
    lookup = moodle.fetch_news_forums

    async def in_reverse(*, token, course_ids):
        return dict(reversed((await lookup(token=token, course_ids=course_ids)).items()))

    moodle.fetch_news_forums = in_reverse
    upserted: list[int] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO course_announcement_courses"):
            upserted.append(parameters[0])

    sa.event.listen(prepared_engine.sync_engine, "before_cursor_execute", record)
    try:
        await _tick(poller, NOW)
    finally:
        sa.event.remove(prepared_engine.sync_engine, "before_cursor_execute", record)

    assert upserted[:3] == [7002, 7003, 7004]


async def test_after_every_holder_fails_once_the_course_waits_its_interval(
    db_session, poller, moodle, holder, term
):
    names = ("alice", "bob", "carol")
    for name in names:
        await holder(name, DS)
        moodle.errors[f"token-{name}"] = MoodleTokenInvalid("invalidtoken")
    await _set_state(db_session, forum_id=30)

    at = NOW
    for _ in names:
        assert await _tick(poller, at) == 1
        at = (await _state(db_session)).next_due_at
    assert sorted(call[1] for call in moodle.polled()) == [f"token-{n}" for n in names]
    # Two retries 15 minutes apart, then a never-posted course's day.
    assert at == NOW + timedelta(minutes=30) + timedelta(days=1)

    for name in names:
        del moodle.errors[f"token-{name}"]
    assert await _tick(poller, at) == 1
    succeeded_at, at = at, (await _state(db_session)).next_due_at
    assert at == succeeded_at + timedelta(days=1)
    for name in names:
        moodle.errors[f"token-{name}"] = MoodleTokenInvalid("invalidtoken")
    assert await _tick(poller, at) == 1
    assert (await _state(db_session)).next_due_at == at + timedelta(minutes=15)


async def test_a_holder_who_sees_no_news_forum_hands_the_course_on(
    db_session, poller, moodle, holder, term
):
    first = await holder("alice", DS)
    second = await holder("bob", DS)
    await _set_state(db_session, holder_user_id=first.id)
    moodle.forums[7001] = 30
    lookup = moodle.fetch_news_forums

    async def hidden_from_alice(*, token, course_ids):
        found = await lookup(token=token, course_ids=course_ids)
        return {} if token == "token-alice" else found

    moodle.fetch_news_forums = hidden_from_alice

    assert await _tick(poller, NOW) == 1
    state = await _state(db_session)
    assert (state.holder_user_id, state.next_due_at) == (second.id, NOW + timedelta(minutes=15))

    assert await _tick(poller, state.next_due_at) == 1
    assert moodle.polled() == [("discussions", "token-bob", 30)]


async def test_a_course_without_a_news_forum_tries_again_tomorrow(
    db_session, poller, moodle, holder, term
):
    await holder("alice", DS)

    assert await _tick(poller, NOW) == 1

    state = await _state(db_session)
    assert (state.forum_id, state.next_due_at) == (None, NOW + timedelta(days=1))
    assert moodle.polled() == []


async def test_an_announcement_of_merged_sections_belongs_to_each_code(
    db_session, poller, moodle, holder, term
):
    await holder("alice", {7001: ("1151CS2006301", "Data Structures CS2006301 EE2006301")})
    moodle.forums[7001] = 30
    moodle.discussions[30] = [_discussion(1, NOW - timedelta(hours=1))]

    await _tick(poller, NOW)

    row = await db_session.get(CourseAnnouncement, "101-901", populate_existing=True)
    assert row.course_codes == ["CS2006301", "EE2006301"]


async def test_a_classifier_down_classifies_later_without_asking_moodle_again(
    db_session, poller, moodle, classifier, holder, term
):
    await holder("alice", DS)
    moodle.forums[7001] = 30
    moodle.discussions[30] = [_discussion(1, NOW - timedelta(hours=1))]
    classifier.down = True

    await _tick(poller, NOW)
    row = await db_session.get(CourseAnnouncement, "101-901", populate_existing=True)
    assert row.route == "unclassified"

    classifier.down = False
    await _tick(poller, NOW + timedelta(minutes=1))

    row = await db_session.get(CourseAnnouncement, "101-901", populate_existing=True)
    assert row.route == "sent"
    assert len(moodle.polled()) == 1


@pytest.mark.parametrize("first", ["poll", "import"])
async def test_the_history_import_and_a_poll_keep_the_later_version_whichever_comes_first(
    db_session, poller, moodle, classifier, holder, term, first
):
    posted, edited = NOW - timedelta(days=2), NOW - timedelta(days=1)
    await holder("alice", DS)
    moodle.forums[7001] = 30
    moodle.discussions[30] = [dataclasses.replace(_discussion(1, posted), modified_at=edited)]
    original = {
        "id": 901,
        "discussion": 101,
        "subject": "Class cancelled",
        "message": "<p>Invented notice 1, before its edit</p>",
        "created": int(posted.timestamp()),
        "modified": int(posted.timestamp()),
        "groupid": -1,
        "timestart": 0,
        "attachment": False,
    }
    course = {"id": 7001, "idnumber": "1151CS2006301", "fullname": "Data Structures"}
    export = read_export([json.dumps({"course": course, "discussions": [original]})])

    async def poll() -> None:
        assert await _tick(poller, NOW) == 1

    async def run_import() -> None:
        await import_history(poller.session_factory, classifier, export)

    for step in (poll, run_import) if first == "poll" else (run_import, poll):
        await step()

    row = await db_session.get(CourseAnnouncement, "101-901", populate_existing=True)
    assert (row.modified_at, row.route, row.course_codes) == (edited, "sent", ["CS2006301"])
    assert row.text == "Class cancelled\n\n<p>Invented notice 1</p>"
    extractions = await db_session.execute(
        select(CourseAnnouncementExtraction.version, CourseAnnouncementExtraction.status)
        .order_by(CourseAnnouncementExtraction.id)
        .execution_options(populate_existing=True)
    )
    replaced = [(posted, "superseded")] if first == "import" else []
    assert [tuple(e) for e in extractions] == [*replaced, (edited, "queued")]


# --- Wiring ----------------------------------------------------------------------


def _sync_worker(settings) -> SyncWorker:
    return SyncWorker(
        session_factory=async_sessionmaker(),
        settings=settings,
        cipher=CredentialCipher.from_settings(settings),
        fetcher=object(),
        course_fetcher=object(),
        worker_id="w",
        budget=RequestBudget(10, observe=lambda: None),
    )


@pytest.mark.parametrize(
    ("url", "with_cipher", "scheduled"),
    [
        ("http://classifier.test:40002", True, True),
        ("http://classifier.test:40002", False, False),
        ("", True, False),
        ("", False, False),
    ],
    ids=["url-and-cipher", "url-only", "cipher-only", "neither"],
)
async def test_the_poll_is_scheduled_only_with_a_classifier_and_credential_keys(
    test_settings, url, with_cipher, scheduled
):
    settings = test_settings.model_copy(update={"announcement_classifier_url": url})
    scheduler = build_scheduler(
        async_sessionmaker(),
        object(),
        settings,
        llm=object(),
        sync_worker=_sync_worker(settings) if with_cipher else None,
    )

    job = scheduler.get_job("course_announcement_poll")
    assert (job is not None) == scheduled
    if scheduled:
        assert job.trigger.interval == timedelta(minutes=1)


async def test_the_poll_sends_on_its_own_budget_and_pauses_with_the_sync_one(test_settings):
    settings = test_settings.model_copy(
        update={"announcement_classifier_url": "http://classifier.test:40002"}
    )
    worker = _sync_worker(settings)

    poller = build_poller(settings, worker)

    assert poller.sync_budget is worker.budget
    assert poller.budget is not worker.budget
    assert poller.fetcher._event_hooks is poller.budget.event_hooks
    assert poller.cipher is worker.cipher


async def test_a_failed_poll_names_no_announcement_text_and_still_classifies(
    db_session, poller, moodle, classifier, holder, term, monkeypatch
):
    await holder("alice", DS)
    moodle.forums[7001] = 30
    moodle.discussions[30] = [_discussion(1, NOW)]
    classifier.down = True
    await _tick(poller, NOW)
    classifier.down = False

    async def refused(*args, **kwargs):
        raise RuntimeError("[parameters: ('Invented secret subject',)]")

    monkeypatch.setattr(poller_module, "_due_courses", refused)

    with pytest.raises(PollTickFailed) as caught:
        await _tick(poller, NOW + timedelta(minutes=15))

    assert caught.value.args == ("RuntimeError",)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    row = await db_session.get(CourseAnnouncement, "101-901", populate_existing=True)
    assert row.route == "sent"


async def test_a_course_that_fails_is_rolled_back_and_deferred_and_the_tick_goes_on(
    db_session, poller, moodle, holder, term, monkeypatch
):
    await holder("alice", {7001: DS[7001], 7002: ("1151CS2007301", "Algorithms")})
    for course_id in (7001, 7002):
        await _set_state(db_session, course_id, forum_id=course_id, next_due_at=NOW)
    # Past the parser, so Postgres refuses the NUL for real.
    moodle.discussions[7001] = [_discussion(1, NOW, subject="Invented\x00 secret")]
    moodle.discussions[7002] = [_discussion(2, NOW)]
    monkeypatch.setattr(poller_module, "logger", structlog.get_logger(poller_module.__name__))

    with capture_logs() as logs:
        assert await _tick(poller, NOW) == 1

    failed = await _state(db_session, 7001)
    assert (failed.latest_post_at, failed.next_due_at) == (None, NOW + timedelta(minutes=15))
    assert (await _state(db_session, 7002)).latest_post_at == NOW
    assert await db_session.get(CourseAnnouncement, "101-901") is None
    [entry] = [e for e in logs if e["event"] == "course_announcements.course_failed"]
    assert entry["moodle_course_id"] == 7001 and entry["error"].isidentifier()
    assert "secret" not in repr(logs)
