"""Assignment reminder generation + stale-job cancellation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, insert, literal, select

from server.academic_calendar.models import SemesterTerm
from server.auth.models import DevicePushToken, PushJob, User, UserDevice
from server.db import build_session_factory
from server.push.reminders import (
    _dedupe_key,
    reminder_key_prefix,
    scan_assignment_reminders,
)
from server.sync.models import (
    UserAssignment,
    UserAssignmentOverride,
    UserCourse,
    UserCourseOverride,
    UserSettingsDocument,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _make_user(session, student_id="b11203058", *, with_device=True):
    user = User(student_id=student_id)
    session.add(user)
    await session.flush()
    if with_device:
        await _add_device(session, user)
    return user


async def _add_device(
    session, user, *, platform="ios", app_version="2.1.0", token=True, **columns
):
    """A device of `user`; by default one the pipeline sends reminders to."""
    device = UserDevice(
        user_id=user.id,
        client_device_id=str(uuid.uuid4()),
        platform=platform,
        app_version=app_version,
        **columns,
    )
    session.add(device)
    await session.flush()
    if token:
        session.add(
            DevicePushToken(
                device_id=device.id,
                provider="apns",
                token_kind="standard",
                token_hash=f"hash-{device.id}",
                token_value=f"tok-{device.id}",
            )
        )
    return device


def _assignment(user, aid=1, due_in_hours=30.0, **kwargs):
    defaults = dict(
        user_id=user.id,
        moodle_course_id=7001,
        moodle_assignment_id=aid,
        course_name="資料結構",
        title=f"HW{aid}",
        due_at=datetime.now(UTC) + timedelta(hours=due_in_hours),
    )
    defaults.update(kwargs)
    return UserAssignment(**defaults)


async def _jobs(session, user_id):
    # populate_existing: the session factory uses expire_on_commit=False,
    # so identity-mapped rows would otherwise mask scan-side updates.
    return (
        (
            await session.execute(
                select(PushJob)
                .where(PushJob.user_id == user_id)
                .order_by(PushJob.fire_at)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )


async def test_creates_jobs_for_default_offsets(
    db_session, prepared_engine, test_settings
):
    user = await _make_user(db_session)
    db_session.add(_assignment(user, due_in_hours=30))
    await db_session.commit()

    created = await scan_assignment_reminders(
        build_session_factory(prepared_engine), test_settings
    )
    assert created == 2  # default offsets [24, 2]

    jobs = await _jobs(db_session, user.id)
    assert len(jobs) == 2
    assert all(j.channel == "assignment" for j in jobs)
    assert jobs[0].scenario == "reminder_24h"
    assert jobs[1].scenario == "reminder_2h"
    due_epoch = int(jobs[0].payload["due_epoch"])
    assert jobs[0].dedupe_key == f"assignment:moodle:1:reminder_24h:{due_epoch}"
    # Copy inputs, not finished copy: the pipeline writes the title and
    # body per recipient, in each device's language.
    assert jobs[0].payload["assignment_title"] == "HW1"
    assert jobs[0].payload["course_name"] == "資料結構"
    assert "title" not in jobs[0].payload
    assert "body" not in jobs[0].payload
    # idempotent re-scan
    created = await scan_assignment_reminders(
        build_session_factory(prepared_engine), test_settings
    )
    assert created == 0


async def test_past_offsets_skipped(db_session, prepared_engine, test_settings):
    user = await _make_user(db_session)
    db_session.add(_assignment(user, due_in_hours=3))  # 24h offset in the past
    await db_session.commit()

    await scan_assignment_reminders(
        build_session_factory(prepared_engine), test_settings
    )
    jobs = await _jobs(db_session, user.id)
    assert [j.scenario for j in jobs] == ["reminder_2h"]


async def test_excluded_assignments_get_no_jobs(
    db_session, prepared_engine, test_settings
):
    user = await _make_user(db_session)
    submitted = _assignment(user, aid=1, provider_is_submitted=True)
    deleted = _assignment(user, aid=2, deleted_at=datetime.now(UTC))
    ignored = _assignment(user, aid=3)
    db_session.add_all([submitted, deleted, ignored])
    await db_session.flush()
    db_session.add(
        UserAssignmentOverride(
            user_id=user.id,
            user_assignment_id=ignored.id,
            local_status="ignored",
        )
    )
    await db_session.commit()

    created = await scan_assignment_reminders(
        build_session_factory(prepared_engine), test_settings
    )
    assert created == 0


async def test_user_settings_control_offsets_and_enabled(
    db_session, prepared_engine, test_settings
):
    user = await _make_user(db_session)
    db_session.add(_assignment(user, due_in_hours=30))
    db_session.add(
        UserSettingsDocument(
            user_id=user.id,
            namespace="notification",
            document={
                "assignments": {
                    "enabled": True,
                    "reminder_offsets_hours": [8],
                }
            },
        )
    )
    await db_session.commit()

    await scan_assignment_reminders(
        build_session_factory(prepared_engine), test_settings
    )
    jobs = await _jobs(db_session, user.id)
    assert [j.scenario for j in jobs] == ["reminder_8h"]

    # Disable → pending jobs are cancelled on next scan.
    doc = (
        await db_session.execute(
            select(UserSettingsDocument).where(
                UserSettingsDocument.user_id == user.id
            )
        )
    ).scalar_one()
    doc.document = {"assignments": {"enabled": False}}
    await db_session.commit()

    await scan_assignment_reminders(
        build_session_factory(prepared_engine), test_settings
    )
    jobs = await _jobs(db_session, user.id)
    assert all(j.status == "cancelled" for j in jobs)


async def test_due_date_change_cancels_old_and_creates_new(
    db_session, prepared_engine, test_settings
):
    user = await _make_user(db_session)
    assignment = _assignment(user, due_in_hours=30)
    db_session.add(assignment)
    await db_session.commit()

    factory = build_session_factory(prepared_engine)
    await scan_assignment_reminders(factory, test_settings)
    old_jobs = await _jobs(db_session, user.id)
    assert len(old_jobs) == 2

    assignment.due_at = assignment.due_at + timedelta(days=2)
    await db_session.commit()

    await scan_assignment_reminders(factory, test_settings)
    jobs = await _jobs(db_session, user.id)
    cancelled = [j for j in jobs if j.status == "cancelled"]
    pending = [j for j in jobs if j.status == "pending"]
    assert len(cancelled) == 2
    assert len(pending) == 2
    new_epoch = int(assignment.due_at.timestamp())
    assert all(str(new_epoch) in j.dedupe_key for j in pending)


async def test_submitted_after_scheduling_cancels_pending(
    db_session, prepared_engine, test_settings
):
    user = await _make_user(db_session)
    assignment = _assignment(user, due_in_hours=30)
    db_session.add(assignment)
    await db_session.commit()

    factory = build_session_factory(prepared_engine)
    await scan_assignment_reminders(factory, test_settings)

    assignment.provider_is_submitted = True
    await db_session.commit()

    await scan_assignment_reminders(factory, test_settings)
    jobs = await _jobs(db_session, user.id)
    assert all(j.status == "cancelled" for j in jobs)


async def test_imminent_valid_job_not_cancelled(
    db_session, prepared_engine, test_settings
):
    """A pending job whose fire_at already passed but whose assignment is
    still eligible must NOT be cancelled (it is about to be delivered)."""
    user = await _make_user(db_session)
    # due in 1.5h → only the 2h offset exists and its fire time has passed
    # by scan time... so create with due 2.05h out, then scan, then time
    # passes: simulate by due 2.01h and re-scan immediately (fire_at is
    # still future). Instead: create job via first scan, then shift the
    # job's fire_at into the past and re-scan.
    assignment = _assignment(user, due_in_hours=30)
    db_session.add(assignment)
    await db_session.commit()

    factory = build_session_factory(prepared_engine)
    await scan_assignment_reminders(factory, test_settings)
    jobs = await _jobs(db_session, user.id)
    target = jobs[0]
    target.fire_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.commit()

    await scan_assignment_reminders(factory, test_settings)
    await db_session.refresh(target)
    assert target.status == "pending"  # still valid — not cancelled


async def test_cancellation_is_user_scoped(
    db_session, prepared_engine, test_settings
):
    """Greptile #1: two users share the same Moodle assignment (same id,
    due, offset → same dedupe_key STRING). User B submitting must cancel
    B's pending reminder even though user A's identical key stays valid."""
    user_a = await _make_user(db_session, student_id="b11203058")
    user_b = await _make_user(db_session, student_id="b11203059")
    shared = dict(aid=42, due_in_hours=30.0)
    a_row = _assignment(user_a, **shared)
    b_row = _assignment(user_b, **shared)
    # Identical due_at → identical due_epoch → identical dedupe_key.
    b_row.due_at = a_row.due_at
    db_session.add_all([a_row, b_row])
    await db_session.commit()

    factory = build_session_factory(prepared_engine)
    await scan_assignment_reminders(factory, test_settings)
    jobs_b = await _jobs(db_session, user_b.id)
    assert jobs_b and all(j.status == "pending" for j in jobs_b)

    # B submits; A stays eligible with the same key string.
    b_row.provider_is_submitted = True
    await db_session.commit()

    await scan_assignment_reminders(factory, test_settings)
    jobs_a = await _jobs(db_session, user_a.id)
    jobs_b = await _jobs(db_session, user_b.id)
    assert all(j.status == "pending" for j in jobs_a)  # A untouched
    assert all(j.status == "cancelled" for j in jobs_b)  # B cancelled


async def test_the_reminder_key_prefix_scopes_to_exactly_one_assignment():
    """What `push/submission_cancel.py` cancels by. Every key of the
    assignment starts with it, whatever the offset, and no key of an
    assignment whose id merely starts with the same digits does."""
    due_epoch = 1_800_000_000
    assert _dedupe_key(12, 24.0, due_epoch).startswith(reminder_key_prefix(12))
    assert _dedupe_key(12, 0.5, due_epoch).startswith(reminder_key_prefix(12))
    assert not _dedupe_key(123, 24.0, due_epoch).startswith(reminder_key_prefix(12))


@pytest.mark.parametrize(
    "device",
    [
        None,
        dict(platform="android"),
        dict(platform="macos"),
        dict(app_version="2.0.2"),
        dict(app_version=None),
        dict(sync_assignment_reminders=False),
        dict(cloud_sync_enabled=False),
        dict(token=False),
        dict(deleted_at=datetime.now(UTC)),
    ],
    ids=[
        "no-device",
        "android",
        "macos",
        "ios-2.0.2",
        "no-version",
        "reminders-off",
        "sync-off",
        "no-token",
        "deleted",
    ],
)
async def test_no_jobs_for_a_user_no_device_can_receive_them_on(
    db_session, prepared_engine, test_settings, device
):
    """Filed anyway, each would settle as failed/no_active_tokens once due."""
    user = await _make_user(db_session, with_device=False)
    if device is not None:
        await _add_device(db_session, user, **device)
    db_session.add(_assignment(user, due_in_hours=30))
    await db_session.commit()

    created = await scan_assignment_reminders(
        build_session_factory(prepared_engine), test_settings
    )

    assert created == 0
    assert await _jobs(db_session, user.id) == []


async def test_one_device_that_can_receive_them_is_enough(
    db_session, prepared_engine, test_settings
):
    user = await _make_user(db_session, with_device=False)
    await _add_device(db_session, user, platform="android")
    await _add_device(db_session, user, platform="ipados")
    db_session.add(_assignment(user, due_in_hours=30))
    await db_session.commit()

    created = await scan_assignment_reminders(
        build_session_factory(prepared_engine), test_settings
    )

    assert created == 2


async def test_pending_jobs_cancelled_once_no_device_can_receive_them(
    db_session, prepared_engine, test_settings
):
    user = await _make_user(db_session, with_device=False)
    device = await _add_device(db_session, user)
    db_session.add(_assignment(user, due_in_hours=30))
    await db_session.commit()
    factory = build_session_factory(prepared_engine)

    await scan_assignment_reminders(factory, test_settings)
    assert [j.status for j in await _jobs(db_session, user.id)] == ["pending"] * 2

    device.sync_assignment_reminders = False
    await db_session.commit()
    await scan_assignment_reminders(factory, test_settings)
    assert [j.status for j in await _jobs(db_session, user.id)] == ["cancelled"] * 2

    # Back on: the same reminders are filed again.
    device.sync_assignment_reminders = True
    await db_session.commit()
    assert await scan_assignment_reminders(factory, test_settings) == 2


async def test_pending_jobs_outlive_a_token_gone_between_scans(
    db_session, prepared_engine, test_settings
):
    # APNs dropped the token, or the app has not registered a new one yet.
    # Cancelled, a reminder due before the next scan was lost even when the
    # app registered a token in time: a scan files no key already due.
    user = await _make_user(db_session, with_device=False)
    device = await _add_device(db_session, user)
    db_session.add(_assignment(user, aid=1, due_in_hours=30))
    await db_session.commit()
    factory = build_session_factory(prepared_engine)
    assert await scan_assignment_reminders(factory, test_settings) == 2

    token = (
        await db_session.execute(
            select(DevicePushToken).where(DevicePushToken.device_id == device.id)
        )
    ).scalar_one()
    token.status = "invalidated"
    # Added meanwhile, an assignment is not filed: nothing could receive it.
    db_session.add(_assignment(user, aid=2, due_in_hours=30))
    await db_session.commit()

    assert await scan_assignment_reminders(factory, test_settings) == 0
    jobs = await _jobs(db_session, user.id)
    assert [(j.payload["moodle_assignment_id"], j.status) for j in jobs] == [
        (1, "pending"),
        (1, "pending"),
    ]

    # Signed out: there is no device left to wait for.
    device.deleted_at = datetime.now(UTC)
    await db_session.commit()
    await scan_assignment_reminders(factory, test_settings)
    assert [j.status for j in await _jobs(db_session, user.id)] == ["cancelled"] * 2


async def test_a_reminder_that_found_no_recipient_is_not_filed_again(
    db_session, prepared_engine, test_settings
):
    """A reminder the pipeline settled as `failed`/`no_active_tokens` no
    longer holds `ux_push_jobs_dedupe_active`, yet the scan does not file it
    again: the pipeline settles a job only once it is due, and the scan
    files no key whose fire time has passed.
    """
    user = await _make_user(db_session)
    # Due in just under two hours: the 2h reminder fell due a minute ago.
    assignment = _assignment(user, due_in_hours=2 - 1 / 60)
    db_session.add(assignment)
    await db_session.flush()
    due_epoch = int(assignment.due_at.timestamp())
    db_session.add(
        PushJob(
            user_id=user.id,
            dedupe_key=_dedupe_key(assignment.moodle_assignment_id, 2.0, due_epoch),
            channel="assignment",
            scenario="reminder_2h",
            fire_at=assignment.due_at - timedelta(hours=2),
            payload={"kind": "assignment_reminder"},
            status="failed",
            last_error="no_active_tokens",
        )
    )
    await db_session.commit()

    created = await scan_assignment_reminders(
        build_session_factory(prepared_engine), test_settings
    )

    assert created == 0
    assert [(j.status, j.last_error) for j in await _jobs(db_session, user.id)] == [
        ("failed", "no_active_tokens")
    ]


async def _ignore_course(
    session, user, *, moodle_course_id=7001, course_no="CS1001301", semester="1141"
):
    """`user` ignores every assignment of a course of theirs, as the course
    override PATCH records it."""
    course = UserCourse(
        user_id=user.id,
        semester=semester,
        course_key=f"client:{semester}:{course_no}",
        course_no=course_no,
        course_name="資料結構",
        moodle_id=f"{semester}{course_no}",
    )
    session.add(course)
    await session.flush()
    override = UserCourseOverride(
        user_id=user.id,
        user_course_id=course.id,
        ignore_assignments=True,
        moodle_course_id=moodle_course_id,
    )
    session.add(override)
    await session.flush()
    return override


async def test_ignored_course_gets_no_jobs_by_its_moodle_course_id(
    db_session, prepared_engine, test_settings
):
    user = await _make_user(db_session)
    await _ignore_course(db_session, user, moodle_course_id=7001)
    db_session.add(_assignment(user, aid=1, moodle_course_id=7001))
    db_session.add(_assignment(user, aid=2, moodle_course_id=7002))
    await db_session.commit()

    created = await scan_assignment_reminders(
        build_session_factory(prepared_engine), test_settings
    )

    assert created == 2
    jobs = await _jobs(db_session, user.id)
    assert {j.payload["moodle_assignment_id"] for j in jobs} == {2}


async def test_ignored_course_skips_upload_placeholders_by_course_number(
    db_session, prepared_engine, test_settings
):
    """An app uploads a placeholder with course id 0 and the course number.
    A row the server fetched is matched by id only, so without the id it
    still gets its reminders."""
    user = await _make_user(db_session)
    await _ignore_course(
        db_session, user, moodle_course_id=None, course_no="CS1001301"
    )
    db_session.add(
        _assignment(user, aid=1, moodle_course_id=0, course_no="CS1001301")
    )
    db_session.add(
        _assignment(user, aid=2, moodle_course_id=0, course_no="CS2002301")
    )
    db_session.add(
        _assignment(user, aid=3, moodle_course_id=7001, course_no="CS1001301")
    )
    await db_session.commit()

    await scan_assignment_reminders(
        build_session_factory(prepared_engine), test_settings
    )

    jobs = await _jobs(db_session, user.id)
    assert {j.payload["moodle_assignment_id"] for j in jobs} == {2, 3}


async def test_ignoring_a_course_cancels_its_filed_reminders_until_undone(
    db_session, prepared_engine, test_settings
):
    user = await _make_user(db_session)
    db_session.add(_assignment(user, aid=1, moodle_course_id=7001))
    await db_session.commit()
    factory = build_session_factory(prepared_engine)
    assert await scan_assignment_reminders(factory, test_settings) == 2

    override = await _ignore_course(db_session, user, moodle_course_id=7001)
    await db_session.commit()
    assert await scan_assignment_reminders(factory, test_settings) == 0
    assert [j.status for j in await _jobs(db_session, user.id)] == ["cancelled"] * 2

    # No longer ignored: the reminders still ahead are filed again.
    override.ignore_assignments = False
    await db_session.commit()
    assert await scan_assignment_reminders(factory, test_settings) == 2
    statuses = sorted(j.status for j in await _jobs(db_session, user.id))
    assert statuses == ["cancelled"] * 2 + ["pending"] * 2


async def test_ignoring_a_course_leaves_other_users_alone(
    db_session, prepared_engine, test_settings
):
    """Two users in the same Moodle course; only the one who ignores it
    loses its reminders."""
    user_a = await _make_user(db_session, student_id="b11203058")
    user_b = await _make_user(db_session, student_id="b11203059")
    await _ignore_course(db_session, user_a, moodle_course_id=7001)
    db_session.add(_assignment(user_a, aid=1, moodle_course_id=7001))
    db_session.add(_assignment(user_b, aid=1, moodle_course_id=7001))
    await db_session.commit()

    await scan_assignment_reminders(
        build_session_factory(prepared_engine), test_settings
    )

    assert await _jobs(db_session, user_a.id) == []
    assert len(await _jobs(db_session, user_b.id)) == 2


EARLIER_TERM = "1141"
CURRENT_TERM = "1142"
NEXT_TERM = "1151"


async def _publish_terms(session, *, with_next=False):
    """Publish a term that has ended and the term in progress. `with_next`
    adds the next term, which the school publishes before it begins, once
    course selection for it opens."""
    today = datetime.now(UTC).date()
    terms = [
        (EARLIER_TERM, today - timedelta(days=300), today - timedelta(days=160)),
        (CURRENT_TERM, today - timedelta(days=100), today + timedelta(days=20)),
    ]
    if with_next:
        terms.append(
            (NEXT_TERM, today + timedelta(days=45), today + timedelta(days=165))
        )
    session.add_all(
        SemesterTerm(code=code, start_date=start, end_date=end)
        for code, start, end in terms
    )
    await session.flush()


def _placeholder(user, aid, course_no):
    """An assignment an app uploaded: course id 0 and the course number."""
    return _assignment(user, aid=aid, moodle_course_id=0, course_no=course_no)


async def _reminded(session, user):
    """The assignments `user` has reminders filed for."""
    return {j.payload["moodle_assignment_id"] for j in await _jobs(session, user.id)}


async def test_a_course_ignored_in_an_earlier_term_keeps_this_terms_placeholders(
    db_session, prepared_engine, test_settings
):
    """A course keeps its number from term to term, and a placeholder
    carries no term. Both apps apply the setting in the current term only,
    so a course taken again after it was ignored gets its reminders."""
    user = await _make_user(db_session)
    await _publish_terms(db_session)
    await _ignore_course(
        db_session, user, course_no="CS1001301", semester=EARLIER_TERM
    )
    db_session.add(_placeholder(user, 1, "CS1001301"))
    await db_session.commit()

    await scan_assignment_reminders(
        build_session_factory(prepared_engine), test_settings
    )

    assert await _reminded(db_session, user) == {1}


async def test_a_course_ignored_in_the_current_term_skips_its_placeholders(
    db_session, prepared_engine, test_settings
):
    user = await _make_user(db_session)
    await _publish_terms(db_session)
    await _ignore_course(
        db_session,
        user,
        moodle_course_id=7101,
        course_no="CS1001301",
        semester=CURRENT_TERM,
    )
    await _ignore_course(
        db_session,
        user,
        moodle_course_id=7002,
        course_no="CS2002301",
        semester=EARLIER_TERM,
    )
    db_session.add(_placeholder(user, 1, "CS1001301"))
    db_session.add(_placeholder(user, 2, "CS2002301"))
    await db_session.commit()

    await scan_assignment_reminders(
        build_session_factory(prepared_engine), test_settings
    )

    assert await _reminded(db_session, user) == {2}


async def test_the_next_terms_courses_synced_early_do_not_change_the_term(
    db_session, prepared_engine, test_settings
):
    """Course selection for the next term opens weeks before it begins, and
    an app showing that term's timetable uploads its courses. The apps keep
    applying the setting in the term in progress until the next one begins,
    whatever the newest term a user has courses in."""
    user = await _make_user(db_session)
    await _publish_terms(db_session, with_next=True)
    await _ignore_course(
        db_session,
        user,
        moodle_course_id=7101,
        course_no="CS1001301",
        semester=CURRENT_TERM,
    )
    await _ignore_course(
        db_session,
        user,
        moodle_course_id=7201,
        course_no="CS2002301",
        semester=NEXT_TERM,
    )
    db_session.add(_placeholder(user, 1, "CS1001301"))
    db_session.add(_placeholder(user, 2, "CS2002301"))
    await db_session.commit()

    await scan_assignment_reminders(
        build_session_factory(prepared_engine), test_settings
    )

    assert await _reminded(db_session, user) == {2}


async def test_fetched_assignments_match_an_ignored_course_by_id_in_any_term(
    db_session, prepared_engine, test_settings
):
    """A numeric Moodle id names one term's course: the rows the server
    fetched for a course ignored in an earlier term stay skipped, and the
    course taken again, under a new id, gets its reminders. The placeholder,
    matched by number, is not skipped."""
    user = await _make_user(db_session)
    await _publish_terms(db_session)
    await _ignore_course(
        db_session,
        user,
        moodle_course_id=7001,
        course_no="CS1001301",
        semester=EARLIER_TERM,
    )
    db_session.add(
        _assignment(user, aid=1, moodle_course_id=7001, course_no="CS1001301")
    )
    db_session.add(
        _assignment(user, aid=2, moodle_course_id=7101, course_no="CS1001301")
    )
    db_session.add(_placeholder(user, 3, "CS1001301"))
    await db_session.commit()

    await scan_assignment_reminders(
        build_session_factory(prepared_engine), test_settings
    )

    assert await _reminded(db_session, user) == {2, 3}


async def test_with_no_term_published_placeholders_match_every_terms_ignores(
    db_session, prepared_engine, test_settings
):
    """Without a calendar the server cannot tell the current term, so a
    placeholder is matched against the courses ignored in any term. Once
    the calendar is published, only the current term's count."""
    user = await _make_user(db_session)
    await _ignore_course(
        db_session, user, course_no="CS1001301", semester=EARLIER_TERM
    )
    db_session.add(_placeholder(user, 1, "CS1001301"))
    await db_session.commit()
    factory = build_session_factory(prepared_engine)

    assert await scan_assignment_reminders(factory, test_settings) == 0

    await _publish_terms(db_session)
    await db_session.commit()
    assert await scan_assignment_reminders(factory, test_settings) == 2


async def test_a_scan_files_more_reminders_than_one_statement_can_carry(
    db_session, prepared_engine, test_settings
):
    """asyncpg allows 32,767 bind parameters per statement and a reminder
    row binds 11. Filed in one insert, 2,979 reminders failed the scan, and
    with it every reminder it should have filed or cancelled."""
    user = await _make_user(db_session)
    db_session.add_all(
        _assignment(user, aid=aid, due_in_hours=30) for aid in range(1, 1501)
    )
    await db_session.commit()
    factory = build_session_factory(prepared_engine)

    assert await scan_assignment_reminders(factory, test_settings) == 3000
    assert len(await _jobs(db_session, user.id)) == 3000
    assert await scan_assignment_reminders(factory, test_settings) == 0


async def test_a_scan_looks_up_more_users_than_a_statement_binds(
    db_session, prepared_engine, test_settings
):
    """The scan looks up the ignored courses, settings and devices of every
    user with an assignment due, and asyncpg allows 32,767 bind parameters
    per statement. Bound one per user, the lookups failed the whole scan
    once about that many users had an assignment due."""
    user = await _make_user(db_session)
    db_session.add(_assignment(user))
    # 32,767 more users with an assignment due and no device to file it for.
    # Postgres generates them: built in Python, the rows take seconds.
    await db_session.execute(
        insert(User).from_select(
            ["id"],
            select(func.gen_random_uuid()).select_from(func.generate_series(1, 32_767)),
        )
    )
    due_at = datetime.now(UTC) + timedelta(hours=30)
    assignment = {
        "user_id": User.id,
        "moodle_course_id": literal(7001),
        "moodle_assignment_id": literal(1),
        "title": literal("HW1"),
        "due_at": literal(due_at, UserAssignment.due_at.type),
    }
    await db_session.execute(
        insert(UserAssignment).from_select(
            list(assignment), select(*assignment.values()).where(User.id != user.id)
        )
    )
    await db_session.commit()

    created = await scan_assignment_reminders(
        build_session_factory(prepared_engine), test_settings
    )

    assert created == 2
    assert len(await _jobs(db_session, user.id)) == 2
