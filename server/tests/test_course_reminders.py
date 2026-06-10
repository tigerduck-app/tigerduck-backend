"""Course reminder generation: occurrence math + scan/cancellation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from server.auth.models import PushJob, User
from server.db import build_session_factory
from server.push.course_reminders import course_occurrences, scan_course_reminders
from server.sync.models import (
    UserCourse,
    UserCourseOverride,
    UserCourseSkippedDate,
    UserSettingsDocument,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

TAIPEI = ZoneInfo("Asia/Taipei")
PERIODS = {"1": "08:10", "3": "10:20", "4": "11:20", "A": "18:25"}


def _utc(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=TAIPEI).astimezone(UTC)


class TestCourseOccurrences:
    # 2026-06-10 is a Wednesday (ISO weekday 3).

    def test_weekly_occurrence_inside_window(self):
        occs = course_occurrences(
            [{"day": 3, "periods": [3, 4]}],
            window_start=datetime(2026, 6, 10, 0, 0, tzinfo=UTC),
            window_end=datetime(2026, 6, 12, 0, 0, tzinfo=UTC),
            tz=TAIPEI,
            period_times=PERIODS,
        )
        # Earliest period (3) wins for the block start.
        assert occs == [_utc(2026, 6, 10, 10, 20)]

    def test_multiple_entries_and_weeks(self):
        occs = course_occurrences(
            [{"day": 3, "periods": [1]}, {"day": 5, "periods": ["A"]}],
            window_start=datetime(2026, 6, 10, 0, 0, tzinfo=UTC),
            window_end=datetime(2026, 6, 18, 0, 0, tzinfo=UTC),
            tz=TAIPEI,
            period_times=PERIODS,
        )
        assert occs == [
            _utc(2026, 6, 10, 8, 10),   # Wed this week
            _utc(2026, 6, 12, 18, 25),  # Fri this week
            _utc(2026, 6, 17, 8, 10),   # Wed next week
        ]

    def test_occurrence_outside_window_excluded(self):
        occs = course_occurrences(
            [{"day": 3, "periods": [3]}],
            window_start=datetime(2026, 6, 10, 3, 0, tzinfo=UTC),  # 11:00 TPE
            window_end=datetime(2026, 6, 11, 0, 0, tzinfo=UTC),
            tz=TAIPEI,
            period_times=PERIODS,
        )
        assert occs == []  # 10:20 TPE already before window_start

    def test_unknown_period_and_day_skipped(self):
        occs = course_occurrences(
            [
                {"day": 3, "periods": [99]},     # unknown period
                {"day": 9, "periods": [3]},      # invalid day
                {"day": 3, "periods": []},       # empty periods
                {"periods": [3]},                # missing day
                "garbage",                       # not a dict
            ],
            window_start=datetime(2026, 6, 10, 0, 0, tzinfo=UTC),
            window_end=datetime(2026, 6, 12, 0, 0, tzinfo=UTC),
            tz=TAIPEI,
            period_times=PERIODS,
        )
        assert occs == []

    def test_empty_schedule(self):
        occs = course_occurrences(
            [],
            window_start=datetime(2026, 6, 10, 0, 0, tzinfo=UTC),
            window_end=datetime(2026, 6, 12, 0, 0, tzinfo=UTC),
            tz=TAIPEI,
            period_times=PERIODS,
        )
        assert occs == []


# --- scan_course_reminders ---


async def _make_user(session, student_id="b11203058"):
    user = User(student_id=student_id)
    session.add(user)
    await session.flush()
    return user


def _course(user, *, course_key="CS2006301", semester="1141", **kwargs):
    defaults = dict(
        user_id=user.id,
        semester=semester,
        course_key=course_key,
        course_no=course_key,
        course_name="資料結構",
        classroom="TR-212",
        schedule_json=[{"day": 1, "periods": [1]}],
    )
    defaults.update(kwargs)
    return UserCourse(**defaults)


def _scan_settings(test_settings, occurrence_local):
    """Pin period 1's start to a controlled local time so the (real-clock)
    scan produces exactly one occurrence inside the window."""
    return test_settings.model_copy(
        update={
            "course_period_start_times": {
                "1": occurrence_local.strftime("%H:%M"),
                "2": (occurrence_local + timedelta(hours=1)).strftime("%H:%M"),
            }
        }
    )


def _occurrence_in(hours: float):
    """A local-time occurrence `hours` from now, plus matching schedule."""
    local = datetime.now(ZoneInfo("Asia/Taipei")) + timedelta(hours=hours)
    return local, [{"day": local.isoweekday(), "periods": [1]}]


async def _jobs(session, user_id):
    return (
        (
            await session.execute(
                select(PushJob)
                .where(PushJob.user_id == user_id)
                .order_by(PushJob.id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )


async def test_creates_job_for_default_offset(
    db_session, prepared_engine, test_settings
):
    user = await _make_user(db_session)
    local, schedule = _occurrence_in(26)
    db_session.add(_course(user, schedule_json=schedule))
    await db_session.commit()

    settings = _scan_settings(test_settings, local)
    factory = build_session_factory(prepared_engine)
    created = await scan_course_reminders(factory, settings)
    assert created == 1  # default offset [10] minutes

    jobs = await _jobs(db_session, user.id)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.channel == "course"
    assert job.scenario == "reminder_10m"
    epoch = job.payload["start_epoch"]
    assert job.dedupe_key == f"course:{job.payload['user_course_id']}:reminder_10m:{epoch}"
    assert "資料結構" in job.payload["title"]
    assert "TR-212" in job.payload["body"]

    # idempotent re-scan
    assert await scan_course_reminders(factory, settings) == 0


async def test_settings_control_offsets_and_disable(
    db_session, prepared_engine, test_settings
):
    user = await _make_user(db_session)
    local, schedule = _occurrence_in(26)
    db_session.add(_course(user, schedule_json=schedule))
    db_session.add(
        UserSettingsDocument(
            user_id=user.id,
            namespace="notification",
            document={"courses": {"enabled": True, "reminder_offsets_minutes": [30]}},
        )
    )
    await db_session.commit()

    settings = _scan_settings(test_settings, local)
    factory = build_session_factory(prepared_engine)
    await scan_course_reminders(factory, settings)
    jobs = await _jobs(db_session, user.id)
    assert [j.scenario for j in jobs] == ["reminder_30m"]

    # Disable → pending jobs cancelled on next scan.
    doc = (
        await db_session.execute(
            select(UserSettingsDocument).where(UserSettingsDocument.user_id == user.id)
        )
    ).scalar_one()
    doc.document = {"courses": {"enabled": False}}
    await db_session.commit()

    await scan_course_reminders(factory, settings)
    jobs = await _jobs(db_session, user.id)
    assert all(j.status == "cancelled" for j in jobs)


async def test_old_semester_hidden_and_excluded_courses(
    db_session, prepared_engine, test_settings
):
    user = await _make_user(db_session)
    local, schedule = _occurrence_in(26)
    current = _course(user, course_key="NEW1", semester="1141", schedule_json=schedule)
    stale = _course(user, course_key="OLD1", semester="1132", schedule_json=schedule)
    hidden = _course(user, course_key="HID1", semester="1141", schedule_json=schedule)
    dropped = _course(
        user,
        course_key="DRP1",
        semester="1141",
        schedule_json=schedule,
        enrollment_status="dropped",
    )
    deleted = _course(
        user,
        course_key="DEL1",
        semester="1141",
        schedule_json=schedule,
        deleted_at=datetime.now(UTC),
    )
    db_session.add_all([current, stale, hidden, dropped, deleted])
    await db_session.flush()
    db_session.add(
        UserCourseOverride(
            user_id=user.id, user_course_id=hidden.id, is_hidden=True
        )
    )
    await db_session.commit()

    settings = _scan_settings(test_settings, local)
    created = await scan_course_reminders(
        build_session_factory(prepared_engine), settings
    )
    assert created == 1
    jobs = await _jobs(db_session, user.id)
    assert jobs[0].payload["user_course_id"] == current.id


async def test_skipped_date_excluded_and_cancelled(
    db_session, prepared_engine, test_settings
):
    user = await _make_user(db_session)
    local, schedule = _occurrence_in(26)
    course = _course(user, schedule_json=schedule)
    db_session.add(course)
    await db_session.commit()

    settings = _scan_settings(test_settings, local)
    factory = build_session_factory(prepared_engine)
    assert await scan_course_reminders(factory, settings) == 1

    db_session.add(
        UserCourseSkippedDate(
            user_id=user.id, user_course_id=course.id, skipped_on=local.date()
        )
    )
    await db_session.commit()

    assert await scan_course_reminders(factory, settings) == 0
    jobs = await _jobs(db_session, user.id)
    assert all(j.status == "cancelled" for j in jobs)


async def test_schedule_change_cancels_old_and_creates_new(
    db_session, prepared_engine, test_settings
):
    user = await _make_user(db_session)
    local, schedule = _occurrence_in(26)
    course = _course(user, schedule_json=schedule)
    db_session.add(course)
    await db_session.commit()

    settings = _scan_settings(test_settings, local)
    factory = build_session_factory(prepared_engine)
    await scan_course_reminders(factory, settings)

    # Same day, later block (period 2 = +1h) → new occurrence epoch.
    course.schedule_json = [{"day": local.isoweekday(), "periods": [2]}]
    await db_session.commit()

    await scan_course_reminders(factory, settings)
    jobs = await _jobs(db_session, user.id)
    cancelled = [j for j in jobs if j.status == "cancelled"]
    pending = [j for j in jobs if j.status == "pending"]
    assert len(cancelled) == 1
    assert len(pending) == 1
    assert pending[0].dedupe_key != cancelled[0].dedupe_key


async def test_imminent_valid_job_not_cancelled(
    db_session, prepared_engine, test_settings
):
    user = await _make_user(db_session)
    local, schedule = _occurrence_in(26)
    db_session.add(_course(user, schedule_json=schedule))
    await db_session.commit()

    settings = _scan_settings(test_settings, local)
    factory = build_session_factory(prepared_engine)
    await scan_course_reminders(factory, settings)

    jobs = await _jobs(db_session, user.id)
    target = jobs[0]
    target.fire_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.commit()

    await scan_course_reminders(factory, settings)
    await db_session.refresh(target)
    assert target.status == "pending"
