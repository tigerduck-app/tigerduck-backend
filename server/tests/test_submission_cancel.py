"""Submitting an assignment must take back what has not gone out yet.

A reminder already delivered cannot be recalled; one still pending can.
The Live Activity is the visible half -- a countdown still ticking on the
lock screen for something already handed in is the complaint this exists
to prevent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from server.auth.models import (
    DevicePushToken,
    PushJob,
    PushJobStatus,
    User,
    UserDevice,
)
from server.push.dedupe import activity_end_key
from server.push.reminders import CHANNEL as REMINDER_CHANNEL
from server.push.reminders import _dedupe_key
from server.push.submission_cancel import cancel_for_submitted
from server.sync.models import UserAssignment

# Every other db_session-based test module in this suite pins the test's
# own event loop to the session-scoped one via this module-level mark --
# see the same comment in test_syncjobs_submissions.py.
pytestmark = pytest.mark.asyncio(loop_scope="session")

NOW = datetime(2026, 9, 11, 0, 0, tzinfo=UTC)
ACTIVITY_SCENARIO = "assignmentUrgent"


async def _make_user(session) -> User:
    user = User()
    session.add(user)
    await session.flush()
    return user


def _reminder_job(
    user: User,
    moodle_assignment_id: int,
    *,
    offset: float = 24.0,
    due_epoch: int = 1_800_000_000,
    **overrides,
) -> PushJob:
    defaults = dict(
        user_id=user.id,
        dedupe_key=_dedupe_key(moodle_assignment_id, offset, due_epoch),
        channel=REMINDER_CHANNEL,
        scenario=f"reminder_{offset:g}h",
        fire_at=NOW + timedelta(hours=1),
        payload={
            "kind": "assignment_reminder",
            "moodle_assignment_id": moodle_assignment_id,
        },
        status=PushJobStatus.pending.value,
    )
    defaults.update(overrides)
    return PushJob(**defaults)


async def _running_activity(
    session, user: User, moodle_assignment_id: int, *, client_device_id: str = "dev-1"
) -> UserDevice:
    """A device with an active live-activity update token scoped to this
    assignment's activity id -- the same signal `pipeline._materialize`
    reads when it later delivers an end push."""
    device = UserDevice(
        user_id=user.id, client_device_id=client_device_id, platform="ios"
    )
    session.add(device)
    await session.flush()
    activity_id = f"{ACTIVITY_SCENARIO}::{moodle_assignment_id}"
    session.add(
        DevicePushToken(
            device_id=device.id,
            provider="apns",
            token_kind="live_activity_update",
            token_hash=f"hash-{client_device_id}-{moodle_assignment_id}",
            token_value=f"tok-{client_device_id}-{moodle_assignment_id}",
            bundle_id="org.ntust.app.TigerDuck",
            scope_key=activity_id,
            status="active",
        )
    )
    await session.flush()
    return device


def _assignment_row(
    user: User,
    moodle_assignment_id: int,
    *,
    title: str = "Untitled",
    course_name: str | None = None,
    due_at: datetime | None = None,
) -> UserAssignment:
    """What the fresh-insert end-job payload is built from -- title,
    course_name and due_at are the only fields it reads."""
    return UserAssignment(
        user_id=user.id,
        moodle_course_id=7001,
        moodle_assignment_id=moodle_assignment_id,
        title=title,
        course_name=course_name,
        due_at=due_at,
    )


def _end_job_row(
    device: UserDevice,
    moodle_assignment_id: int,
    *,
    status: str,
    fire_at: datetime,
    payload: dict,
) -> PushJob:
    """A `live_activity_end` job as `/live-activities/register` would have
    filed it -- a full client snapshot already in `payload`, unlike this
    module's own fresh-insert path."""
    activity_id = f"{ACTIVITY_SCENARIO}::{moodle_assignment_id}"
    return PushJob(
        user_id=device.user_id,
        device_id=device.id,
        dedupe_key=activity_end_key(device.id, activity_id),
        channel="schedule",
        scenario="activityEnd",
        fire_at=fire_at,
        payload=payload,
        status=status,
    )


def _end_job_key(device_id, moodle_assignment_id: int) -> str:
    return activity_end_key(device_id, f"{ACTIVITY_SCENARIO}::{moodle_assignment_id}")


async def _end_jobs(session, device_id, moodle_assignment_id: int) -> list[PushJob]:
    return (
        (
            await session.execute(
                select(PushJob).where(
                    PushJob.dedupe_key == _end_job_key(device_id, moodle_assignment_id)
                )
            )
        )
        .scalars()
        .all()
    )


# 1. a pending reminder job for a now-submitted assignment is cancelled
async def test_pending_reminder_for_submitted_assignment_is_cancelled(db_session):
    user = await _make_user(db_session)
    job = _reminder_job(user, 42)
    db_session.add(job)
    await db_session.commit()

    cancelled, created = await cancel_for_submitted(
        db_session, user_id=user.id, moodle_assignment_ids={42}, now=NOW
    )

    await db_session.refresh(job)
    assert (cancelled, created) == (1, 0)
    assert job.status == PushJobStatus.cancelled.value
    assert job.cancelled_at == NOW


# 2. an ALREADY SENT job for the same assignment is left alone --
# ux_push_jobs_dedupe_active covers the sent state, and flipping a sent
# job to cancelled would make the delivery record disagree with what
# actually happened.
async def test_already_sent_job_is_left_alone(db_session):
    user = await _make_user(db_session)
    job = _reminder_job(
        user,
        42,
        status=PushJobStatus.sent.value,
        sent_at=NOW - timedelta(hours=1),
    )
    db_session.add(job)
    await db_session.commit()

    cancelled, _ = await cancel_for_submitted(
        db_session, user_id=user.id, moodle_assignment_ids={42}, now=NOW
    )

    await db_session.refresh(job)
    assert cancelled == 0
    assert job.status == PushJobStatus.sent.value
    assert job.cancelled_at is None


# 3. a pending job for a DIFFERENT, still-unsubmitted assignment survives
async def test_pending_job_for_a_different_assignment_survives(db_session):
    user = await _make_user(db_session)
    submitted_job = _reminder_job(user, 42)
    other_job = _reminder_job(user, 99)
    db_session.add_all([submitted_job, other_job])
    await db_session.commit()

    cancelled, _ = await cancel_for_submitted(
        db_session, user_id=user.id, moodle_assignment_ids={42}, now=NOW
    )

    await db_session.refresh(submitted_job)
    await db_session.refresh(other_job)
    assert cancelled == 1
    assert submitted_job.status == PushJobStatus.cancelled.value
    assert other_job.status == PushJobStatus.pending.value


# 4. a running live activity for the submitted assignment gets a
# live_activity_end job
async def test_running_live_activity_gets_an_end_job(db_session):
    user = await _make_user(db_session)
    device = await _running_activity(db_session, user, 42)
    db_session.add(
        _assignment_row(
            user, 42, title="Essay 3", course_name="Algorithms",
            due_at=NOW + timedelta(hours=5),
        )
    )
    await db_session.commit()

    cancelled, created = await cancel_for_submitted(
        db_session, user_id=user.id, moodle_assignment_ids={42}, now=NOW
    )

    assert (cancelled, created) == (0, 1)
    jobs = await _end_jobs(db_session, device.id, 42)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.channel == "schedule"
    assert job.status == PushJobStatus.pending.value
    assert job.fire_at == NOW
    assert job.device_id == device.id
    assert job.payload["kind"] == "live_activity_end"
    assert job.payload["activity_id"] == f"{ACTIVITY_SCENARIO}::42"
    assert job.payload["source_id"] == "42"
    # A push the client can actually decode: LiveActivitySnapshot has five
    # non-optional fields (scenario, title, subtitle, accentHex, sourceId),
    # and the pipeline forwards everything but the three routing keys
    # above straight through as the content-state -- so all five must be
    # present or ActivityKit discards the push and the activity is never
    # dismissed.
    assert job.payload["scenario"] == ACTIVITY_SCENARIO
    assert job.payload["title"] == "Essay 3"
    assert job.payload["subtitle"] == "Algorithms"
    assert isinstance(job.payload["accentHex"], int)
    assert job.payload["sourceId"] == "42"


# 5. calling it twice is idempotent -- the second call creates no new end
# job and cancels nothing further
async def test_calling_twice_is_idempotent(db_session):
    user = await _make_user(db_session)
    reminder = _reminder_job(user, 42)
    db_session.add(reminder)
    device = await _running_activity(db_session, user, 42)
    db_session.add(_assignment_row(user, 42, due_at=NOW + timedelta(hours=5)))
    await db_session.commit()

    first = await cancel_for_submitted(
        db_session, user_id=user.id, moodle_assignment_ids={42}, now=NOW
    )
    await db_session.commit()

    second = await cancel_for_submitted(
        db_session, user_id=user.id, moodle_assignment_ids={42}, now=NOW
    )
    await db_session.commit()

    assert first == (1, 1)
    assert second == (0, 0)
    jobs = await _end_jobs(db_session, device.id, 42)
    assert len(jobs) == 1


# 6. an empty id set is a no-op that touches nothing. Deliberately gives
# the call something a broken "empty means match everything" filter
# WOULD touch, so the assertion is not vacuous.
async def test_empty_id_set_is_a_no_op(db_session):
    user = await _make_user(db_session)
    reminder = _reminder_job(user, 42)
    db_session.add(reminder)
    device = await _running_activity(db_session, user, 42)
    await db_session.commit()

    cancelled, created = await cancel_for_submitted(
        db_session, user_id=user.id, moodle_assignment_ids=set(), now=NOW
    )

    await db_session.refresh(reminder)
    assert (cancelled, created) == (0, 0)
    assert reminder.status == PushJobStatus.pending.value
    assert await _end_jobs(db_session, device.id, 42) == []


# --- Supplementary cross-user isolation coverage ------------------------
#
# Beyond the six cases above, but every query this module writes opens
# with a `user_id ==` filter, and `test_syncjobs_submissions.py` holds
# itself to proving that predicate rather than assuming it (see
# test_apply_does_not_touch_another_users_matching_assignment_id there).
# Matching that bar here rather than only testing it incidentally.


async def test_reminder_for_a_different_user_is_not_touched(db_session):
    mine = await _make_user(db_session)
    theirs = await _make_user(db_session)
    my_job = _reminder_job(mine, 42)
    their_job = _reminder_job(theirs, 42)
    db_session.add_all([my_job, their_job])
    await db_session.commit()

    cancelled, _ = await cancel_for_submitted(
        db_session, user_id=mine.id, moodle_assignment_ids={42}, now=NOW
    )

    await db_session.refresh(my_job)
    await db_session.refresh(their_job)
    assert cancelled == 1
    assert my_job.status == PushJobStatus.cancelled.value
    assert their_job.status == PushJobStatus.pending.value


async def test_running_activity_for_a_different_user_is_not_touched(db_session):
    mine = await _make_user(db_session)
    theirs = await _make_user(db_session)
    my_device = await _running_activity(db_session, mine, 42, client_device_id="mine")
    their_device = await _running_activity(
        db_session, theirs, 42, client_device_id="theirs"
    )
    db_session.add(_assignment_row(mine, 42, due_at=NOW + timedelta(hours=5)))
    await db_session.commit()

    cancelled, created = await cancel_for_submitted(
        db_session, user_id=mine.id, moodle_assignment_ids={42}, now=NOW
    )

    assert (cancelled, created) == (0, 1)
    assert len(await _end_jobs(db_session, my_device.id, 42)) == 1
    assert await _end_jobs(db_session, their_device.id, 42) == []


# --- Supplementary end-job conflict coverage -----------------------------
#
# The upsert has two behaviours neither of the six tests above exercises:
# a job that already fired must not be resurrected (the end-job mirror of
# test 2's reminder guard), and a still-pending job being accelerated must
# keep the client's real snapshot rather than gain this module's own
# minimal one.


async def test_sent_end_job_is_not_resurrected(db_session):
    """An end job that already fired must be left alone. The same trap as
    test 2, on the other job kind: resetting a sent job to pending would
    re-fire a push that already reached the device and rewrite delivery
    history to boot."""
    user = await _make_user(db_session)
    device = await _running_activity(db_session, user, 42)
    original_payload = {
        "kind": "live_activity_end",
        "activity_id": f"{ACTIVITY_SCENARIO}::42",
        "source_id": "42",
        "scenario": ACTIVITY_SCENARIO,
        "title": "Already dismissed",
        "subtitle": "",
        "accentHex": 999,
        "sourceId": "42",
    }
    sent_job = _end_job_row(
        device,
        42,
        status=PushJobStatus.sent.value,
        fire_at=NOW - timedelta(hours=1),
        payload=original_payload,
    )
    db_session.add(sent_job)
    await db_session.commit()

    cancelled, created = await cancel_for_submitted(
        db_session, user_id=user.id, moodle_assignment_ids={42}, now=NOW
    )

    await db_session.refresh(sent_job)
    assert created == 0
    assert sent_job.status == PushJobStatus.sent.value
    assert sent_job.fire_at == NOW - timedelta(hours=1)
    assert sent_job.payload == original_payload


async def test_accelerating_a_pending_end_job_preserves_its_payload(db_session):
    """A pending end job already filed for the original due date must have
    its fire time moved to now, and must keep the real snapshot the client
    uploaded at registration rather than being overwritten with this
    module's own minimal fresh-insert payload."""
    user = await _make_user(db_session)
    device = await _running_activity(db_session, user, 42)
    original_target = NOW + timedelta(hours=6)
    rich_payload = {
        "kind": "live_activity_end",
        "activity_id": f"{ACTIVITY_SCENARIO}::42",
        "source_id": "42",
        "scenario": ACTIVITY_SCENARIO,
        "title": "Real client title",
        "subtitle": "Real client subtitle",
        "accentHex": 0x4A90E2,
        "sourceId": "42",
        "countdownTarget": original_target.isoformat(),
    }
    pending_job = _end_job_row(
        device,
        42,
        status=PushJobStatus.pending.value,
        fire_at=original_target,
        payload=rich_payload,
    )
    db_session.add(pending_job)
    await db_session.commit()

    cancelled, created = await cancel_for_submitted(
        db_session, user_id=user.id, moodle_assignment_ids={42}, now=NOW
    )

    await db_session.refresh(pending_job)
    assert created == 0
    assert pending_job.status == PushJobStatus.pending.value
    assert pending_job.fire_at == NOW
    assert pending_job.payload == rich_payload


async def test_missing_assignment_row_does_not_file_an_undeliverable_job(db_session):
    """No prior end job to accelerate, and no assignment row to build a
    fresh snapshot from -- filing one anyway would only ever produce a
    push the client cannot decode. Nothing is created, and nothing raises."""
    user = await _make_user(db_session)
    device = await _running_activity(db_session, user, 42)
    await db_session.commit()
    # Deliberately no assignment row for id 42.

    cancelled, created = await cancel_for_submitted(
        db_session, user_id=user.id, moodle_assignment_ids={42}, now=NOW
    )

    assert (cancelled, created) == (0, 0)
    assert await _end_jobs(db_session, device.id, 42) == []
