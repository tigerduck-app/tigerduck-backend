"""Course reminder scanning: job creation, cancellation, and exclusions.

The occurrence math these build on is covered by
test_course_occurrences.py.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete, event, func, insert, literal, select, update

from server.academic_calendar.models import CourseChange, SemesterTerm
from server.auth.models import DevicePushToken, PushJob, User, UserDevice
from server.db import build_session_factory
from server.push import course_reminders as course_reminders_module
from server.push.apns_client import RecordingSender
from server.push.course_reminders import scan_course_reminders
from server.push.pipeline import PushPipelineWorker, run_push_tick
from server.push.router import PushRouter
from server.sync.models import (
    UserCourse,
    UserCourseOverride,
    UserCourseSkippedDate,
    UserSettingsDocument,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


# --- scan_course_reminders ---


async def _make_user(session, student_id="b11203058", *, platform="ios"):
    """A user with one device on `platform` the pipeline can push to — the
    scan files nothing for an account no device of which could receive it."""
    user = User(student_id=student_id)
    session.add(user)
    await session.flush()
    device = UserDevice(
        user_id=user.id, client_device_id=f"dev-{student_id}", platform=platform
    )
    session.add(device)
    await session.flush()
    session.add(
        DevicePushToken(
            device_id=device.id,
            provider="fcm" if platform == "android" else "apns",
            token_kind="standard",
            token_hash=f"hash-{student_id}",
            token_value=f"tok-{student_id}",
        )
    )
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
    # The title is written per recipient at send time; the start time and
    # room read the same in every language.
    assert job.payload["course_name"] == "資料結構"
    assert job.payload["body"] == f"{local:%H:%M} · TR-212"
    assert "title" not in job.payload

    # idempotent re-scan
    assert await scan_course_reminders(factory, settings) == 0


async def test_creates_job_for_a_schedule_in_the_form_the_apps_upload(
    db_session, prepared_engine, test_settings
):
    user = await _make_user(db_session)
    local, _ = _occurrence_in(26)
    db_session.add(_course(user, schedule_json={str(local.isoweekday()): ["1"]}))
    await db_session.commit()

    settings = _scan_settings(test_settings, local)
    created = await scan_course_reminders(build_session_factory(prepared_engine), settings)

    assert created == 1
    [job] = await _jobs(db_session, user.id)
    assert job.payload["body"] == f"{local:%H:%M} · TR-212"


async def test_no_job_for_an_account_with_only_android_devices(
    db_session, prepared_engine, test_settings
):
    # Android posts its own class reminders, so the pipeline sends these to
    # no Android device. Filed anyway, each would settle as a failed job.
    user = await _make_user(db_session, platform="android")
    local, schedule = _occurrence_in(26)
    db_session.add(_course(user, schedule_json=schedule))
    await db_session.commit()

    settings = _scan_settings(test_settings, local)
    factory = build_session_factory(prepared_engine)

    assert await scan_course_reminders(factory, settings) == 0
    assert await _jobs(db_session, user.id) == []



@pytest.mark.parametrize("platform", ["watchos", "windows", "wearos", "web"])
async def test_no_job_for_an_account_with_no_iphone_or_ipad(
    db_session, prepared_engine, test_settings, platform
):
    # The pipeline sends class reminders to iPhone and iPad only, so a
    # client added later is not sent them by default; the scan files none
    # for an account it could not deliver to.
    user = await _make_user(db_session, platform=platform)
    local, schedule = _occurrence_in(26)
    db_session.add(_course(user, schedule_json=schedule))
    await db_session.commit()

    settings = _scan_settings(test_settings, local)
    factory = build_session_factory(prepared_engine)

    assert await scan_course_reminders(factory, settings) == 0
    assert await _jobs(db_session, user.id) == []


async def test_a_filed_reminder_outlives_a_token_gone_between_scans(
    db_session, prepared_engine, test_settings
):
    # APNs dropped the token, or the app has not registered a new one yet.
    # Cancelled, a reminder due before the next scan was lost even when the
    # app registered a token in time: a scan files no key already due.
    user = await _make_user(db_session)
    local, schedule = _occurrence_in(26)
    db_session.add(_course(user, schedule_json=schedule))
    await db_session.commit()
    settings = _scan_settings(test_settings, local)
    factory = build_session_factory(prepared_engine)
    assert await scan_course_reminders(factory, settings) == 1

    device = (
        await db_session.execute(select(UserDevice).where(UserDevice.user_id == user.id))
    ).scalar_one()
    token = (
        await db_session.execute(
            select(DevicePushToken).where(DevicePushToken.device_id == device.id)
        )
    ).scalar_one()
    token.status = "invalidated"
    # Added meanwhile, a course is not filed: nothing could receive it yet.
    db_session.add(_course(user, course_key="MA1001", schedule_json=schedule))
    await db_session.commit()

    assert await scan_course_reminders(factory, settings) == 0
    assert [j.status for j in await _jobs(db_session, user.id)] == ["pending"]

    # Signed out: there is no device left to wait for.
    device.deleted_at = datetime.now(UTC)
    await db_session.commit()
    await scan_course_reminders(factory, settings)
    assert [j.status for j in await _jobs(db_session, user.id)] == ["cancelled"]


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


@pytest.mark.parametrize(
    ("section", "expected"),
    [
        ({}, (True, [10.0])),
        ({"enabled": "false"}, (False, [10.0])),
        ({"enabled": 1}, (False, [10.0])),
        ({"reminder_offsets_minutes": [True, 15, 2.5, "5"]}, (True, [15.0, 2.5])),
    ],
)
async def test_settings_take_only_a_boolean_switch_and_numeric_offsets(
    db_session, test_settings, section, expected
):
    """A hand-edited document must not turn reminders on with the string
    "false", or file a 1-minute reminder for a JSON true (#104)."""
    user = await _make_user(db_session)
    db_session.add(
        UserSettingsDocument(
            user_id=user.id, namespace="notification", document={"courses": section}
        )
    )
    await db_session.flush()
    settings = test_settings.model_copy(
        update={"course_reminder_default_offsets_minutes": [10]}
    )

    prefs = await course_reminders_module._course_prefs(db_session, {user.id}, settings)

    assert prefs[user.id] == expected


async def test_old_semester_hidden_and_excluded_courses(
    db_session, prepared_engine, test_settings
):
    user = await _make_user(db_session)
    local, schedule = _occurrence_in(26)
    current = _course(user, course_key="NEW1", semester="1141", schedule_json=schedule)
    stale = _course(user, course_key="OLD1", semester="1132", schedule_json=schedule)
    dropped = _course(
        user,
        course_key="DRP1",
        semester="1141",
        schedule_json=schedule,
        enrollment_status="dropped",
    )
    # Removed courses are hard-deleted, so they don't exist in the DB at
    # all — only old-semester and dropped-status courses remain.
    db_session.add_all([current, stale, dropped])
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


async def test_user_added_semester_string_cannot_mask_portal_courses(
    db_session, prepared_engine, test_settings
):
    """A user_added course with a free-form "9999" semester must not win
    the latest-semester guard over real portal courses."""
    user = await _make_user(db_session)
    local, schedule = _occurrence_in(26)
    # Insert the user_added row FIRST: with no ORDER BY it tends to come
    # back first, which is exactly the ordering that broke the original
    # single-pass portal-first guard.
    weird = _course(
        user,
        course_key="CUSTOM1",
        course_no=None,
        semester="9999",
        source="user_added",
        schedule_json=schedule,
    )
    db_session.add(weird)
    await db_session.flush()
    portal = _course(user, course_key="CS1", semester="1141", schedule_json=schedule)
    db_session.add(portal)
    await db_session.commit()

    settings = _scan_settings(test_settings, local)
    created = await scan_course_reminders(
        build_session_factory(prepared_engine), settings
    )
    jobs = await _jobs(db_session, user.id)
    assert created == len(jobs)
    assert {j.payload["user_course_id"] for j in jobs} == {portal.id}


async def test_a_course_added_by_hand_counts_toward_the_latest_semester(
    db_session, prepared_engine, test_settings
):
    """With no term published, the user's latest semester stands in for it.
    The course upload keys a course added by hand `client:{semester}:...`
    under a real term, as it does a roster one, so it counts the same."""
    user = await _make_user(db_session)
    local, schedule = _occurrence_in(26)
    db_session.add(
        _course(
            user, course_key="client:1141:CS1", course_no="CS1", semester="1141",
            schedule_json=schedule,
        )
    )
    added = _course(
        user, course_key="client:1142:GE1", course_no="GE1", semester="1142",
        source="user_added", schedule_json=schedule,
    )
    db_session.add(added)
    await db_session.commit()

    created = await scan_course_reminders(
        build_session_factory(prepared_engine), _scan_settings(test_settings, local)
    )
    jobs = await _jobs(db_session, user.id)
    assert created == len(jobs) == 1
    assert jobs[0].payload["user_course_id"] == added.id


async def test_a_course_added_under_no_real_term_leaves_the_latest_semester_alone(
    db_session, prepared_engine, test_settings
):
    """The initial upload takes any key and semester, so a `user_added`
    course keyed `client:9999:...` under "9999" must not outrank the
    portal courses either."""
    user = await _make_user(db_session)
    local, schedule = _occurrence_in(26)
    db_session.add(
        _course(
            user, course_key="client:9999:GE1", course_no="GE1", semester="9999",
            source="user_added", schedule_json=schedule,
        )
    )
    await db_session.flush()
    portal = _course(
        user, course_key="client:1141:CS1", course_no="CS1", semester="1141",
        schedule_json=schedule,
    )
    db_session.add(portal)
    await db_session.commit()

    created = await scan_course_reminders(
        build_session_factory(prepared_engine), _scan_settings(test_settings, local)
    )
    jobs = await _jobs(db_session, user.id)
    assert created == len(jobs) == 1
    assert jobs[0].payload["user_course_id"] == portal.id


# --- holiday suppression ---
#
# Apple guards twice: the app stays quiet AND the backend never sends. This
# is the backend half, and it is the only one that works while the app is
# not running to suppress anything.


def _holiday(local, *, name_zh="中秋節", name_en="Mid-Autumn"):
    from server.academic_calendar.models import AcademicHoliday

    day = local.date()
    return AcademicHoliday(
        name_zh=name_zh, name_en=name_en, start_date=day, end_date=day
    )


async def test_no_job_when_the_class_falls_on_a_holiday(
    db_session, prepared_engine, test_settings
):
    user = await _make_user(db_session)
    local, schedule = _occurrence_in(26)
    db_session.add(_course(user, schedule_json=schedule))
    db_session.add(_holiday(local))
    await db_session.commit()

    settings = _scan_settings(test_settings, local)
    factory = build_session_factory(prepared_engine)
    assert await scan_course_reminders(factory, settings) == 0
    assert await _jobs(db_session, user.id) == []


async def test_a_holiday_on_another_day_changes_nothing(
    db_session, prepared_engine, test_settings
):
    user = await _make_user(db_session)
    local, schedule = _occurrence_in(26)
    db_session.add(_course(user, schedule_json=schedule))
    db_session.add(_holiday(local + timedelta(days=3)))
    await db_session.commit()

    settings = _scan_settings(test_settings, local)
    factory = build_session_factory(prepared_engine)
    assert await scan_course_reminders(factory, settings) == 1


async def test_opting_in_restores_the_reminder(
    db_session, prepared_engine, test_settings
):
    """The user said "I have class that day" — the server has to believe
    them, or the toggle in the app would be a lie on Apple."""
    from server.academic_calendar.models import UserHolidayOverride

    user = await _make_user(db_session)
    local, schedule = _occurrence_in(26)
    db_session.add(_course(user, schedule_json=schedule))
    holiday = _holiday(local)
    db_session.add(holiday)
    await db_session.flush()
    db_session.add(
        UserHolidayOverride(user_id=user.id, holiday_id=holiday.id, notify=True)
    )
    await db_session.commit()

    settings = _scan_settings(test_settings, local)
    factory = build_session_factory(prepared_engine)
    assert await scan_course_reminders(factory, settings) == 1


async def test_another_users_opt_in_does_not_leak(
    db_session, prepared_engine, test_settings
):
    from server.academic_calendar.models import UserHolidayOverride

    quiet_user = await _make_user(db_session, student_id="b11203058")
    loud_user = await _make_user(db_session, student_id="b11203059")
    local, schedule = _occurrence_in(26)
    db_session.add(_course(quiet_user, schedule_json=schedule))
    db_session.add(_course(loud_user, schedule_json=schedule))
    holiday = _holiday(local)
    db_session.add(holiday)
    await db_session.flush()
    db_session.add(
        UserHolidayOverride(user_id=loud_user.id, holiday_id=holiday.id, notify=True)
    )
    await db_session.commit()

    settings = _scan_settings(test_settings, local)
    factory = build_session_factory(prepared_engine)
    assert await scan_course_reminders(factory, settings) == 1
    assert await _jobs(db_session, quiet_user.id) == []
    assert len(await _jobs(db_session, loud_user.id)) == 1


# --- term dates ---
#
# Each day's reminders go to the courses of the term both apps show that
# day, the one that began most recently, and only while a published term
# runs, from its first day of classes through its last.


def _term(code, start, end):
    return SemesterTerm(code=code, start_date=start, end_date=end)


def _two_class_days():
    """A class at one time on two days in a row, 12 and 36 hours from now,
    both inside the scan's 48-hour window."""
    first = datetime.now(ZoneInfo("Asia/Taipei")) + timedelta(hours=12)
    days = (first, first + timedelta(days=1))
    return first, [{"day": day.isoweekday(), "periods": [1]} for day in days]


def _class_day(job):
    starts_at = datetime.fromisoformat(job.payload["starts_at"])
    return starts_at.astimezone(ZoneInfo("Asia/Taipei")).date()


async def _scan_two_class_days(
    db_session, prepared_engine, test_settings, *, semesters, terms
):
    """Scans one course of each of `semesters`, all held on two days in a
    row, with `terms` built from the first day. Returns that day and the
    (semester, day) of each reminder filed."""
    user = await _make_user(db_session)
    first, schedule = _two_class_days()
    db_session.add_all(
        _course(
            user, course_key=f"CS{semester}", semester=semester, schedule_json=schedule
        )
        for semester in semesters
    )
    db_session.add_all(terms(first.date()))
    await db_session.commit()

    settings = _scan_settings(test_settings, first)
    factory = build_session_factory(prepared_engine)
    created = await scan_course_reminders(factory, settings)
    jobs = await _jobs(db_session, user.id)
    assert created == len(jobs)
    filed = sorted((job.payload["semester"], _class_day(job)) for job in jobs)
    return first.date(), filed


async def test_a_term_rings_through_its_last_day_and_not_after(
    db_session, prepared_engine, test_settings
):
    first, filed = await _scan_two_class_days(
        db_session,
        prepared_engine,
        test_settings,
        semesters=["1151"],
        terms=lambda first: [_term("1151", first - timedelta(days=120), first)],
    )
    assert filed == [("1151", first)]


async def test_a_term_rings_from_its_first_day_and_not_before(
    db_session, prepared_engine, test_settings
):
    """Its courses are synced, but the term opens on the second day."""
    first, filed = await _scan_two_class_days(
        db_session,
        prepared_engine,
        test_settings,
        semesters=["1152"],
        terms=lambda first: [
            _term("1152", first + timedelta(days=1), first + timedelta(days=120))
        ],
    )
    assert filed == [("1152", first + timedelta(days=1))]


async def test_course_selection_leaves_the_next_terms_courses_for_its_start(
    db_session, prepared_engine, test_settings
):
    """Course selection serves the next term weeks before it opens, and the
    app syncs those courses at once. The term running keeps its reminders."""
    first, filed = await _scan_two_class_days(
        db_session,
        prepared_engine,
        test_settings,
        semesters=["1151", "1152"],
        terms=lambda first: [
            _term("1151", first - timedelta(days=100), first + timedelta(days=14)),
            _term("1152", first + timedelta(days=40), first + timedelta(days=160)),
        ],
    )
    assert filed == [("1151", first), ("1151", first + timedelta(days=1))]


async def test_the_next_term_takes_over_on_its_first_day(
    db_session, prepared_engine, test_settings
):
    first, filed = await _scan_two_class_days(
        db_session,
        prepared_engine,
        test_settings,
        semesters=["1151", "1152"],
        terms=lambda first: [
            _term("1151", first - timedelta(days=120), first),
            _term("1152", first + timedelta(days=1), first + timedelta(days=120)),
        ],
    )
    assert filed == [("1151", first), ("1152", first + timedelta(days=1))]


async def test_a_term_without_dates_gets_no_reminders(
    db_session, prepared_engine, test_settings
):
    """1152 has no dates yet, so the apps still show 1151, the last term
    published: its courses ring while it runs, and nothing after it."""
    first, filed = await _scan_two_class_days(
        db_session,
        prepared_engine,
        test_settings,
        semesters=["1151", "1152"],
        terms=lambda first: [_term("1151", first - timedelta(days=120), first)],
    )
    assert filed == [("1151", first)]


@contextmanager
def _semesters_read():
    """The semesters of the courses loaded from the database meanwhile."""
    read: set[str] = set()

    def record(course, _context):
        read.add(course.semester)

    event.listen(UserCourse, "load", record)
    try:
        yield read
    finally:
        event.remove(UserCourse, "load", record)


async def test_a_scan_reads_no_course_of_a_term_that_ended(
    db_session, prepared_engine, test_settings
):
    with _semesters_read() as read:
        first, filed = await _scan_two_class_days(
            db_session,
            prepared_engine,
            test_settings,
            semesters=["1151", "1152"],
            terms=lambda first: [
                _term("1151", first - timedelta(days=300), first - timedelta(days=180)),
                _term("1152", first - timedelta(days=120), first + timedelta(days=60)),
            ],
        )
    assert filed == [("1152", first), ("1152", first + timedelta(days=1))]
    assert read == {"1152"}


async def test_a_scan_across_a_terms_first_day_reads_both_terms(
    db_session, prepared_engine, test_settings
):
    with _semesters_read() as read:
        first, filed = await _scan_two_class_days(
            db_session,
            prepared_engine,
            test_settings,
            semesters=["1142", "1151", "1152"],
            terms=lambda first: [
                _term("1151", first - timedelta(days=120), first),
                _term("1152", first + timedelta(days=1), first + timedelta(days=120)),
            ],
        )
    assert filed == [("1151", first), ("1152", first + timedelta(days=1))]
    assert read == {"1151", "1152"}


async def test_a_reminder_the_day_before_a_terms_first_day_is_filed(
    db_session, prepared_engine, test_settings
):
    """Scanned in the break, a day before the term's first class: the
    reminder fires in the break too, and only the new term is read."""
    user = await _make_user(db_session)
    local, schedule = _occurrence_in(26)
    day = local.date()
    db_session.add_all(
        [
            _course(user, course_key="OLD1", semester="1151", schedule_json=schedule),
            _course(user, course_key="NEW1", semester="1152", schedule_json=schedule),
            _term("1151", day - timedelta(days=200), day - timedelta(days=30)),
            _term("1152", day, day + timedelta(days=120)),
        ]
    )
    await db_session.commit()

    settings = _scan_settings(test_settings, local).model_copy(
        update={"course_reminder_default_offsets_minutes": [24 * 60]}
    )
    with _semesters_read() as read:
        created = await scan_course_reminders(
            build_session_factory(prepared_engine), settings
        )
    assert created == 1
    [job] = await _jobs(db_session, user.id)
    assert job.payload["semester"] == "1152"
    assert job.fire_at.astimezone(ZoneInfo("Asia/Taipei")).date() == day - timedelta(
        days=1
    )
    assert read == {"1152"}


async def test_with_no_term_published_a_scan_reads_every_term(
    db_session, prepared_engine, test_settings
):
    """The latest synced term rings, so every term's courses are read."""
    with _semesters_read() as read:
        first, filed = await _scan_two_class_days(
            db_session,
            prepared_engine,
            test_settings,
            semesters=["1132", "1141"],
            terms=lambda first: [],
        )
    assert filed == [("1141", first), ("1141", first + timedelta(days=1))]
    assert read == {"1132", "1141"}


async def test_a_scan_inside_a_break_reads_no_course(
    db_session, prepared_engine, test_settings
):
    with _semesters_read() as read:
        _first, filed = await _scan_two_class_days(
            db_session,
            prepared_engine,
            test_settings,
            semesters=["1151", "1152"],
            terms=lambda first: [
                _term("1151", first - timedelta(days=200), first - timedelta(days=10)),
                _term("1152", first + timedelta(days=30), first + timedelta(days=150)),
            ],
        )
    assert filed == []
    assert read == set()


async def test_an_opted_in_holiday_in_the_break_stays_quiet(
    db_session, prepared_engine, test_settings
):
    """"還要上課？" switched back on for a holiday in the winter break puts
    the holiday back, not days the term does not run on."""
    from server.academic_calendar.models import UserHolidayOverride

    user = await _make_user(db_session)
    local, schedule = _occurrence_in(26)
    day = local.date()
    db_session.add(_course(user, semester="1151", schedule_json=schedule))
    db_session.add_all(
        [
            _term("1151", day - timedelta(days=120), day - timedelta(days=1)),
            _term("1152", day + timedelta(days=7), day + timedelta(days=127)),
        ]
    )
    holiday = _holiday(local, name_zh="寒假", name_en="Winter break")
    db_session.add(holiday)
    await db_session.flush()
    db_session.add(
        UserHolidayOverride(user_id=user.id, holiday_id=holiday.id, notify=True)
    )
    await db_session.commit()

    settings = _scan_settings(test_settings, local)
    factory = build_session_factory(prepared_engine)
    assert await scan_course_reminders(factory, settings) == 0
    assert await _jobs(db_session, user.id) == []


async def test_dates_published_after_a_reminder_was_filed_cancel_it(
    db_session, prepared_engine, test_settings
):
    """With no term published the latest synced term rings, as before; dates
    published since that leave the class out cancel the reminder filed."""
    user = await _make_user(db_session)
    local, schedule = _occurrence_in(26)
    db_session.add(_course(user, semester="1152", schedule_json=schedule))
    await db_session.commit()
    settings = _scan_settings(test_settings, local)
    factory = build_session_factory(prepared_engine)
    assert await scan_course_reminders(factory, settings) == 1

    opening = local.date() + timedelta(days=1)
    db_session.add(_term("1152", opening, opening + timedelta(days=120)))
    await db_session.commit()

    assert await scan_course_reminders(factory, settings) == 0
    assert [job.status for job in await _jobs(db_session, user.id)] == ["cancelled"]


async def test_a_scan_files_more_reminders_than_one_statement_can_carry(
    db_session, prepared_engine, test_settings
):
    """asyncpg allows 32,767 bind parameters per statement and a reminder
    row binds 11. Filed in one insert, 2,979 reminders failed the scan, and
    with it every reminder it should have filed or cancelled."""
    user = await _make_user(db_session)
    local, schedule = _occurrence_in(26)
    db_session.add_all(
        _course(user, course_key=f"CS{n:07d}", schedule_json=schedule)
        for n in range(3000)
    )
    await db_session.commit()
    factory = build_session_factory(prepared_engine)
    settings = _scan_settings(test_settings, local)

    assert await scan_course_reminders(factory, settings) == 3000
    assert len(await _jobs(db_session, user.id)) == 3000
    assert await scan_course_reminders(factory, settings) == 0


async def test_a_scan_looks_up_skipped_dates_for_more_courses_than_a_statement_binds(
    db_session, prepared_engine, test_settings
):
    """The scan looks up the skipped dates of every user's current courses,
    and asyncpg allows 32,767 bind parameters per statement. Bound one per
    course, the lookup failed the whole scan from 32,768 courses on."""
    user = await _make_user(db_session)
    local, schedule = _occurrence_in(26)
    skipped_course = _course(user, course_key="CS1000001", schedule_json=schedule)
    kept_course = _course(user, course_key="CS1000002", schedule_json=schedule)
    other_user = User(student_id="b11203059")
    db_session.add_all([skipped_course, kept_course, other_user])
    await db_session.flush()
    db_session.add(
        UserCourseSkippedDate(
            user_id=user.id, user_course_id=skipped_course.id, skipped_on=local.date()
        )
    )
    await db_session.execute(
        insert(UserCourse),
        [
            {
                "user_id": other_user.id,
                "semester": "1141",
                "course_key": f"CS{n:07d}",
                "course_no": f"CS{n:07d}",
                "course_name": "資料結構",
                "schedule_json": schedule,
            }
            for n in range(32_767)
        ],
    )
    await db_session.commit()
    factory = build_session_factory(prepared_engine)
    settings = _scan_settings(test_settings, local)

    assert await scan_course_reminders(factory, settings) == 1
    [job] = await _jobs(db_session, user.id)
    assert job.payload["user_course_id"] == kept_course.id


async def test_a_scan_looks_up_more_users_than_a_statement_binds(
    db_session, prepared_engine, test_settings
):
    """The scan looks up the settings and devices of every user with a
    timetable, and asyncpg allows 32,767 bind parameters per statement.
    Bound one per user, the lookups failed the whole scan once about that
    many users had one."""
    user = await _make_user(db_session)
    local, schedule = _occurrence_in(26)
    db_session.add(_course(user, schedule_json=schedule))
    # 32,767 more users with that class and no device to file it for.
    # Postgres generates them: built in Python, the rows take seconds.
    await db_session.execute(
        insert(User).from_select(
            ["id"],
            select(func.gen_random_uuid()).select_from(func.generate_series(1, 32_767)),
        )
    )
    course = {
        "user_id": User.id,
        "semester": literal("1141"),
        "course_key": literal("CS2006301"),
        "course_no": literal("CS2006301"),
        "course_name": literal("資料結構"),
        "schedule_json": literal(schedule, UserCourse.schedule_json.type),
    }
    await db_session.execute(
        insert(UserCourse).from_select(
            list(course), select(*course.values()).where(User.id != user.id)
        )
    )
    await db_session.commit()
    factory = build_session_factory(prepared_engine)
    settings = _scan_settings(test_settings, local)

    assert await scan_course_reminders(factory, settings) == 1
    assert len(await _jobs(db_session, user.id)) == 1


async def test_a_cleared_custom_name_gives_the_reminder_the_course_name(
    db_session, prepared_engine, test_settings
):
    """A cleared name is stored as "" so the apps hear of it; the reminder
    goes back to the course's own name."""
    local, schedule = _occurrence_in(26)
    user_ids = {}
    for student_id, names in (("b11203001", {"zh": "早八"}), ("b11203002", {"zh": "", "en": ""})):
        user = await _make_user(db_session, student_id)
        course = _course(user, schedule_json=schedule)
        db_session.add(course)
        await db_session.flush()
        db_session.add(
            UserCourseOverride(user_id=user.id, user_course_id=course.id, custom_names=names)
        )
        user_ids[student_id] = user.id
    await db_session.commit()

    factory = build_session_factory(prepared_engine)
    assert await scan_course_reminders(factory, _scan_settings(test_settings, local)) == 2
    for student_id, name in (("b11203001", "早八"), ("b11203002", "資料結構")):
        (job,) = await _jobs(db_session, user_ids[student_id])
        assert job.payload["course_name"] == name


async def test_a_class_reminder_is_titled_in_each_devices_language(
    db_session, prepared_engine, test_settings
):
    user = await _make_user(db_session)
    english = (await db_session.execute(select(UserDevice))).scalar_one()
    english.locale = "en-US"
    chinese = UserDevice(
        user_id=user.id, client_device_id="ipad", platform="ipados", locale="zh-Hant-TW"
    )
    db_session.add(chinese)
    await db_session.flush()
    db_session.add(
        DevicePushToken(
            device_id=chinese.id,
            provider="apns",
            token_kind="standard",
            token_hash="hash-ipad",
            token_value="tok-ipad",
        )
    )
    local, schedule = _occurrence_in(0.5)
    db_session.add(_course(user, schedule_json=schedule))
    await db_session.commit()
    factory = build_session_factory(prepared_engine)
    settings = _scan_settings(test_settings, local).model_copy(
        update={"course_reminder_default_offsets_minutes": [10.0]}
    )
    assert await scan_course_reminders(factory, settings) == 1
    (job,) = await _jobs(db_session, user.id)
    job.fire_at = job.available_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()

    apple = RecordingSender()
    await run_push_tick(
        PushPipelineWorker(
            session_factory=factory,
            settings=settings,
            router=PushRouter(apple=apple, android=RecordingSender()),
            worker_id="course-test-worker",
        )
    )

    sent = {r.device_token: r.message for r in apple.requests}
    body = f"{local:%H:%M} · TR-212"
    assert {token: m["aps"]["alert"] for token, m in sent.items()} == {
        "tok-b11203058": {"title": "Class starting soon: 資料結構", "body": body},
        "tok-ipad": {"title": "即將上課：資料結構", "body": body},
    }
    assert all("course_name" not in m for m in sent.values())


async def test_a_class_reminder_filed_before_send_time_titles_goes_out_as_filed(
    db_session, prepared_engine, test_settings
):
    user = await _make_user(db_session)
    device = (await db_session.execute(select(UserDevice))).scalar_one()
    device.locale = "en-US"
    local, schedule = _occurrence_in(0.5)
    db_session.add(_course(user, schedule_json=schedule))
    await db_session.commit()
    factory = build_session_factory(prepared_engine)
    settings = _scan_settings(test_settings, local).model_copy(
        update={"course_reminder_default_offsets_minutes": [10.0]}
    )
    assert await scan_course_reminders(factory, settings) == 1
    (job,) = await _jobs(db_session, user.id)
    filed = {key: value for key, value in job.payload.items() if key != "course_name"}
    filed |= {"title": "上課提醒：資料結構", "body": "10 分鐘後上課 · TR-212"}
    job.payload = filed
    job.fire_at = job.available_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()

    apple = RecordingSender()
    await run_push_tick(
        PushPipelineWorker(
            session_factory=factory,
            settings=settings,
            router=PushRouter(apple=apple, android=RecordingSender()),
            worker_id="course-test-worker",
        )
    )

    (request,) = apple.requests
    assert request.message["aps"]["alert"] == {
        "title": "上課提醒：資料結構",
        "body": "10 分鐘後上課 · TR-212",
    }


# --- the school's course changes (tigerduck-backend#177) ---


def _course_change(local, **fields) -> CourseChange:
    """A change to CS2006301 on `local`'s day, in term 1141."""
    return CourseChange(
        **{
            "term_code": "1141",
            "course_no": "CS2006301",
            "kind": "cancelled",
            "mode": "day",
            "start_date": local.date(),
            "end_date": local.date(),
            "excluded_weekdays": [],
            "created_by": "ops@example.com",
            "updated_by": "ops@example.com",
            **fields,
        }
    )


def _term_around(local) -> SemesterTerm:
    return SemesterTerm(
        code="1141",
        start_date=local.date() - timedelta(days=30),
        end_date=local.date() + timedelta(days=30),
    )


def _moved(local, minutes: int) -> datetime:
    """`local` moved by `minutes`, toward noon so it stays on its day."""
    return local + timedelta(minutes=minutes if local.hour < 12 else -minutes)


async def _course_with_change(db_session, local, schedule, *changes):
    user = await _make_user(db_session)
    db_session.add_all([_term_around(local), _course(user, schedule_json=schedule)])
    await db_session.flush()
    db_session.add_all(changes)
    await db_session.commit()
    return user


async def _send_due(factory, settings, db_session, user):
    """Makes the user's filed reminders due and runs the pipeline once."""
    for job in await _jobs(db_session, user.id):
        job.fire_at = job.available_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    apple = RecordingSender()
    await run_push_tick(
        PushPipelineWorker(
            session_factory=factory,
            settings=settings,
            router=PushRouter(apple=apple, android=RecordingSender()),
            worker_id="course-test-worker",
        )
    )
    return apple


@pytest.mark.parametrize("periods", [None, ["1"]])
async def test_no_reminder_for_a_cancelled_class(
    db_session, prepared_engine, test_settings, periods
):
    local, schedule = _occurrence_in(26)
    user = await _course_with_change(
        db_session, local, schedule, _course_change(local, periods=periods)
    )

    factory = build_session_factory(prepared_engine)
    assert await scan_course_reminders(factory, _scan_settings(test_settings, local)) == 0
    assert await _jobs(db_session, user.id) == []


async def test_a_cancelled_meeting_leaves_the_days_other_meeting(
    db_session, prepared_engine, test_settings
):
    local, _ = _occurrence_in(26)
    schedule = [
        {"day": local.isoweekday(), "periods": [1]},
        {"day": local.isoweekday(), "periods": [2]},
    ]
    user = await _course_with_change(
        db_session, local, schedule, _course_change(local, periods=["2"])
    )

    factory = build_session_factory(prepared_engine)
    assert await scan_course_reminders(factory, _scan_settings(test_settings, local)) == 1
    (job,) = await _jobs(db_session, user.id)
    assert job.payload["start_epoch"] == int(local.replace(second=0, microsecond=0).timestamp())


async def test_a_changed_room_and_start_reach_the_reminder(
    db_session, prepared_engine, test_settings
):
    local, schedule = _occurrence_in(26)
    start = _moved(local, 40).replace(second=0, microsecond=0)
    user = await _course_with_change(
        db_session,
        local,
        schedule,
        _course_change(local, kind="room", room="IB-302"),
        _course_change(local, kind="start", start_time=f"{start:%H:%M}", periods=["1"]),
        _course_change(local, kind="end", end_time="23:59"),
    )

    factory = build_session_factory(prepared_engine)
    assert await scan_course_reminders(factory, _scan_settings(test_settings, local)) == 1
    (job,) = await _jobs(db_session, user.id)
    assert job.fire_at == start - timedelta(minutes=10)
    assert job.payload["body"] == f"{start:%H:%M} · IB-302"
    assert job.payload["start_epoch"] == int(start.timestamp())
    assert job.dedupe_key.endswith(f":{int(start.timestamp())}")


async def test_a_change_reaches_a_holiday_the_user_put_back(
    db_session, prepared_engine, test_settings
):
    from server.academic_calendar.models import UserHolidayOverride

    local, schedule = _occurrence_in(26)
    user = await _course_with_change(
        db_session, local, schedule, _course_change(local, kind="room", room="IB-302")
    )
    holiday = _holiday(local)
    db_session.add(holiday)
    await db_session.flush()
    db_session.add(UserHolidayOverride(user_id=user.id, holiday_id=holiday.id, notify=True))
    await db_session.commit()

    factory = build_session_factory(prepared_engine)
    settings = _scan_settings(test_settings, local)
    assert await scan_course_reminders(factory, settings) == 1
    (job,) = await _jobs(db_session, user.id)
    assert job.payload["body"] == f"{local:%H:%M} · IB-302"

    apple = await _send_due(factory, settings, db_session, user)

    (request,) = apple.requests
    assert request.message["aps"]["alert"]["body"] == f"{local:%H:%M} · IB-302"


async def test_a_change_to_periods_the_course_no_longer_meets_in_is_left_out(
    db_session, prepared_engine, test_settings
):
    local, schedule = _occurrence_in(26)
    user = await _course_with_change(
        db_session, local, schedule,
        _course_change(local, kind="room", room="IB-302", periods=["1", "2"]),
    )

    factory = build_session_factory(prepared_engine)
    assert await scan_course_reminders(factory, _scan_settings(test_settings, local)) == 1
    (job,) = await _jobs(db_session, user.id)
    assert "IB-302" not in job.payload["body"]


async def test_a_start_changed_after_filing_moves_the_reminder(
    db_session, prepared_engine, test_settings
):
    local, schedule = _occurrence_in(26)
    user = await _course_with_change(db_session, local, schedule)
    factory = build_session_factory(prepared_engine)
    settings = _scan_settings(test_settings, local)
    assert await scan_course_reminders(factory, settings) == 1
    start = _moved(local, 40).replace(second=0, microsecond=0)
    db_session.add(_course_change(local, kind="start", start_time=f"{start:%H:%M}"))
    await db_session.commit()

    assert await scan_course_reminders(factory, settings) == 1
    first, moved = await _jobs(db_session, user.id)
    assert first.status == "cancelled"
    assert (moved.status, moved.fire_at) == ("pending", start - timedelta(minutes=10))


async def test_a_class_moved_to_later_today_keeps_its_reminder(
    db_session, prepared_engine, test_settings
):
    """A class whose own start has passed but whose new one has not."""
    now = datetime.now(ZoneInfo("Asia/Taipei"))
    if not 1 <= now.hour <= 21:
        pytest.skip("needs room on both sides of now on one day")
    local = now - timedelta(minutes=30)
    start = (now + timedelta(minutes=50)).replace(second=0, microsecond=0)
    user = await _course_with_change(
        db_session,
        local,
        [{"day": local.isoweekday(), "periods": [1]}],
        _course_change(local, kind="start", start_time=f"{start:%H:%M}"),
    )

    factory = build_session_factory(prepared_engine)
    assert await scan_course_reminders(factory, _scan_settings(test_settings, local)) == 1
    (job,) = await _jobs(db_session, user.id)
    assert job.fire_at == start - timedelta(minutes=10)


async def test_a_room_changed_after_filing_reaches_the_reminder_at_send(
    db_session, prepared_engine, test_settings
):
    local, schedule = _occurrence_in(0.5)
    user = await _course_with_change(db_session, local, schedule)
    factory = build_session_factory(prepared_engine)
    settings = _scan_settings(test_settings, local)
    assert await scan_course_reminders(factory, settings) == 1
    db_session.add(_course_change(local, kind="room", room="IB-302"))
    await db_session.commit()

    apple = await _send_due(factory, settings, db_session, user)

    (request,) = apple.requests
    assert request.message["aps"]["alert"]["body"] == f"{local:%H:%M} · IB-302"


@pytest.mark.parametrize(
    "fields",
    [{"kind": "cancelled"}, {"kind": "start", "start_time": None}],
)
async def test_a_change_after_filing_holds_the_reminder_at_send(
    db_session, prepared_engine, test_settings, fields
):
    """Cancelled, or moved to a start the scan files a reminder of its own for."""
    local, schedule = _occurrence_in(0.5)
    user = await _course_with_change(db_session, local, schedule)
    factory = build_session_factory(prepared_engine)
    settings = _scan_settings(test_settings, local)
    assert await scan_course_reminders(factory, settings) == 1
    if fields["kind"] == "start":
        fields = {**fields, "start_time": f"{_moved(local, 40):%H:%M}"}
    db_session.add(_course_change(local, **fields))
    await db_session.commit()

    apple = await _send_due(factory, settings, db_session, user)

    assert apple.requests == []
    (job,) = await _jobs(db_session, user.id)
    assert (job.status, job.last_error) == ("cancelled", "class_changed")


async def test_a_reminder_is_dropped_when_its_check_fails(
    db_session, prepared_engine, test_settings, monkeypatch
):
    local, schedule = _occurrence_in(0.5)
    user = await _course_with_change(db_session, local, schedule)
    factory = build_session_factory(prepared_engine)
    settings = _scan_settings(test_settings, local)
    assert await scan_course_reminders(factory, settings) == 1

    async def unreachable(*args, **kwargs):
        raise ConnectionError("database gone")

    monkeypatch.setattr(course_reminders_module, "load_course_changes", unreachable)
    apple = await _send_due(factory, settings, db_session, user)

    assert apple.requests == []
    (job,) = await _jobs(db_session, user.id)
    assert (job.status, job.last_error) == ("cancelled", "unverified_before_send")


@pytest.mark.parametrize("gone", ["removed", "dropped"])
async def test_a_reminder_for_a_removed_course_is_dropped_at_send(
    db_session, prepared_engine, test_settings, gone
):
    local, schedule = _occurrence_in(0.5)
    user = await _course_with_change(db_session, local, schedule)
    factory = build_session_factory(prepared_engine)
    settings = _scan_settings(test_settings, local)
    assert await scan_course_reminders(factory, settings) == 1
    if gone == "removed":
        await db_session.execute(delete(UserCourse))
    else:
        await db_session.execute(update(UserCourse).values(enrollment_status="dropped"))
    await db_session.commit()

    apple = await _send_due(factory, settings, db_session, user)

    assert apple.requests == []
    (job,) = await _jobs(db_session, user.id)
    assert job.status == "cancelled"
