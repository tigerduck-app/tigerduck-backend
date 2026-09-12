"""Assignment reminder generation + stale-job cancellation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from server.auth.models import PushJob, User
from server.db import build_session_factory
from server.push.reminders import scan_assignment_reminders
from server.sync.models import (
    UserAssignment,
    UserAssignmentOverride,
    UserSettingsDocument,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _make_user(session, student_id="b11203058"):
    user = User(student_id=student_id)
    session.add(user)
    await session.flush()
    return user


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
