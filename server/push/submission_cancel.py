"""Take back what a just-submitted assignment no longer needs.

`apply_submission_status` writes the fact that an assignment flipped from
unsubmitted to submitted; this module reacts to it. A reminder that has
not gone out yet must be cancelled -- sending it would tell a student to
do something they already did. A Live Activity still counting down
toward the same due date must be told to end now instead of at the
original deadline, since the countdown it is showing is no longer true.

Kept separate from `push/reminders.py`: `scan_assignment_reminders` is a
300-second full scan across every user, while this is an immediate,
narrow reaction to the handful of assignment ids one sync just found
were submitted. Different trigger, different input shape, different
failure semantics -- folding this in would grow the scan function a
branch only the sync path ever takes.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import structlog
from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.models import (
    PUSH_JOB_DEDUPE_ACTIVE_STATUSES,
    DevicePushToken,
    PushJob,
    PushJobStatus,
    PushTokenKind,
    PushTokenStatus,
    UserDevice,
)
from server.auth.schemas import ScheduleScenario
from server.push.dedupe import SCHEDULE_CHANNEL
from server.push.dedupe import activity_end_key
from server.push.dedupe import activity_id as _activity_id
from server.push.reminders import CHANNEL as REMINDER_CHANNEL
from server.push.reminders import reminder_key_prefix
from server.sync.models import UserAssignment

logger = structlog.get_logger(__name__)

# The user's in-app accent-color preference is client-only state, never
# uploaded to the backend, so it cannot be reconstructed at cancel time.
# Harmless for an end push specifically: the activity is being dismissed,
# not re-rendered, so the exact color has no user-visible effect. Named
# rather than a bare literal so the reason travels with the value.
_FALLBACK_ACCENT_HEX = 0


def _activity_id_for_assignment(moodle_assignment_id: int) -> str:
    """The same `{scenario}::{source_id}` id the client composes for an
    assignment's Live Activity (`composedActivityId`) and the backend
    composes for a schedule-driven start (`dedupe.activity_id`). The
    assignment's source_id is its Moodle id as a string -- the client
    resolver sets `sourceId: assignment.assignmentId`, which is
    `String(record.assignId)`.
    """
    return _activity_id(
        str(moodle_assignment_id), ScheduleScenario.assignment_urgent.value
    )


def _fresh_end_payload(
    assignment: UserAssignment, activity_id: str, moodle_assignment_id: int
) -> dict:
    """A decodable `LiveActivitySnapshot` content-state for an end job with
    no prior row to accelerate.

    `job_payloads.py` strips only the three routing keys (`kind`,
    `activity_id`, `source_id`) and forwards everything else as the
    content-state, so every non-optional `LiveActivitySnapshot` field --
    `scenario`, `title`, `subtitle`, `accentHex`, `sourceId` -- must be
    present here, or ActivityKit discards the push outright and the
    activity is never dismissed: exactly the failure this module exists to
    prevent. `countdownTarget` is optional on the client but cheap and
    accurate from the assignment row, so it rides along too; the rest
    (`locationText`, `instructor`, `progressStart`, `deepLink`) are left
    out and decode as `nil`.
    """
    return {
        "kind": "live_activity_end",
        "activity_id": activity_id,
        "source_id": str(moodle_assignment_id),
        "scenario": ScheduleScenario.assignment_urgent.value,
        "title": assignment.title,
        "subtitle": assignment.course_name or "",
        "accentHex": _FALLBACK_ACCENT_HEX,
        "sourceId": str(moodle_assignment_id),
        "countdownTarget": (
            assignment.due_at.isoformat() if assignment.due_at else None
        ),
    }


async def cancel_for_submitted(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    moodle_assignment_ids: set[int],
    now: datetime,
) -> tuple[int, int]:
    """React to assignments that just flipped from unsubmitted to submitted.

    Cancels any still-pending reminder jobs for them and ends any Live
    Activity still counting down toward their due date. Returns
    `(cancelled_reminder_jobs, live_activity_end_jobs_created)`.

    An empty `moodle_assignment_ids` is a no-op, checked explicitly rather
    than left to fall out of empty `or_`/`in_` filters matching nothing --
    that is a true but incidental fact about the query builders below,
    not something this function should quietly depend on.
    """
    if not moodle_assignment_ids:
        return (0, 0)

    cancelled = await _cancel_pending_reminders(
        session, user_id=user_id, moodle_assignment_ids=moodle_assignment_ids, now=now
    )
    created = await _end_running_activities(
        session, user_id=user_id, moodle_assignment_ids=moodle_assignment_ids, now=now
    )
    if cancelled:
        # The ORM-level status/cancelled_at writes above are still pending
        # in the session; a caller that reads one of these rows back
        # (e.g. via session.refresh) before an intervening flush would
        # see the expired-then-reloaded pre-write value, the same hazard
        # `apply_submission_status` documents for its own writes.
        await session.flush()
    return (cancelled, created)


async def _cancel_pending_reminders(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    moodle_assignment_ids: set[int],
    now: datetime,
) -> int:
    """Cancel pending assignment-reminder jobs for these assignments.

    Only `status == pending` ever qualifies: `ux_push_jobs_dedupe_active`
    also covers `sent` (and `processing`/`partial_failed`), and flipping
    an already-sent job to cancelled would make the delivery record
    disagree with what actually happened.
    """
    prefixes = [reminder_key_prefix(aid) for aid in moodle_assignment_ids]
    jobs = (
        (
            await session.execute(
                select(PushJob)
                .where(
                    PushJob.user_id == user_id,
                    PushJob.channel == REMINDER_CHANNEL,
                    PushJob.status == PushJobStatus.pending.value,
                    or_(
                        *(
                            PushJob.dedupe_key.startswith(prefix, autoescape=True)
                            for prefix in prefixes
                        )
                    ),
                )
                # A row the pipeline is concurrently claiming must not be
                # cancelled here -- same reasoning as
                # `scan_assignment_reminders`'s identical lock.
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    for job in jobs:
        job.status = PushJobStatus.cancelled.value
        job.cancelled_at = now
    return len(jobs)


async def _end_running_activities(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    moodle_assignment_ids: set[int],
    now: datetime,
) -> int:
    """File an immediate `live_activity_end` job for every device still
    running a Live Activity for one of these assignments.

    "Still running" is read the same way the pipeline reads it when it
    later delivers the end push: an active `live_activity_update` token
    scoped to the activity id (`push/pipeline.py`'s `_materialize`,
    `is_activity_end` branch, which also requires the token unexpired --
    matched here for the same reason). A device with no such token has
    nothing to end, so it gets no job: an unconditional end job would
    give the pipeline nothing to deliver it to and just fail loudly
    (`pipeline.py`'s `missing_activity_id` / empty-token-query path).
    """
    assignment_by_activity_id = {
        _activity_id_for_assignment(aid): aid for aid in moodle_assignment_ids
    }

    rows = (
        await session.execute(
            select(DevicePushToken.device_id, DevicePushToken.scope_key)
            .join(UserDevice, UserDevice.id == DevicePushToken.device_id)
            .where(
                UserDevice.user_id == user_id,
                UserDevice.deleted_at.is_(None),
                DevicePushToken.token_kind == PushTokenKind.live_activity_update.value,
                DevicePushToken.status == PushTokenStatus.active.value,
                DevicePushToken.scope_key.in_(
                    list(assignment_by_activity_id.keys())
                ),
                or_(
                    DevicePushToken.expires_at.is_(None),
                    DevicePushToken.expires_at > now,
                ),
            )
        )
    ).all()
    if not rows:
        return 0

    # A device can hold at most one active token per scope_key (the
    # register route invalidates the previous one on re-registration), but
    # de-duplicate defensively -- the "created" count below assumes each
    # target is processed once.
    distinct_targets = {(device_id, activity_id) for device_id, activity_id in rows}
    targets = [
        (device_id, activity_id, activity_end_key(device_id, activity_id))
        for device_id, activity_id in distinct_targets
    ]

    existing_keys = set(
        (
            await session.execute(
                select(PushJob.dedupe_key).where(
                    PushJob.user_id == user_id,
                    PushJob.dedupe_key.in_([key for _, _, key in targets]),
                    PushJob.status.in_(PUSH_JOB_DEDUPE_ACTIVE_STATUSES),
                )
            )
        )
        .scalars()
        .all()
    )

    # A key with no active row is a fresh insert, and only a fresh insert
    # needs a real snapshot -- the accelerate path (an active row already
    # exists) leaves `payload` untouched via `set_` below, preserving
    # whatever `register_live_activity` originally stored. Fetched once,
    # outside the loop, and only for the ids that actually need it.
    fresh_assignment_ids = {
        assignment_by_activity_id[activity_id]
        for _, activity_id, dedupe_key in targets
        if dedupe_key not in existing_keys
    }
    assignments_by_id: dict[int, UserAssignment] = {}
    if fresh_assignment_ids:
        assignment_rows = (
            (
                await session.execute(
                    select(UserAssignment).where(
                        UserAssignment.user_id == user_id,
                        UserAssignment.moodle_assignment_id.in_(
                            fresh_assignment_ids
                        ),
                        UserAssignment.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        assignments_by_id = {
            row.moodle_assignment_id: row for row in assignment_rows
        }

    created = 0
    for device_id, activity_id, dedupe_key in targets:
        moodle_assignment_id = assignment_by_activity_id[activity_id]
        is_fresh = dedupe_key not in existing_keys
        counts_as_created = False

        if is_fresh:
            assignment = assignments_by_id.get(moodle_assignment_id)
            if assignment is None:
                # No prior row to accelerate and nothing to build a
                # decodable snapshot from -- inserting anyway would only
                # ever produce a push ActivityKit discards on arrival
                # (job_payloads.py forwards everything but the three
                # routing keys as the content-state). Skipping is the
                # correct outcome here, not a silent one: logged so a
                # cancellation that quietly did nothing in the field
                # leaves a trace.
                logger.warning(
                    "push.submission_cancel.assignment_row_missing",
                    moodle_assignment_id=moodle_assignment_id,
                )
                continue
            payload = _fresh_end_payload(assignment, activity_id, moodle_assignment_id)
            counts_as_created = True
        else:
            # Discarded by Postgres on the conflict path below (`set_`
            # excludes `payload`) -- kept minimal rather than repeating
            # `_fresh_end_payload`'s lookup for a value that is never used.
            payload = {
                "kind": "live_activity_end",
                "activity_id": activity_id,
                "source_id": str(moodle_assignment_id),
            }

        stmt = (
            pg_insert(PushJob)
            .values(
                user_id=user_id,
                device_id=device_id,
                dedupe_key=dedupe_key,
                channel=SCHEDULE_CHANNEL,
                scenario="activityEnd",
                fire_at=now,
                payload=payload,
            )
            .on_conflict_do_update(
                index_elements=["user_id", "dedupe_key"],
                # ux_push_jobs_dedupe_active is partial (active statuses);
                # repeat its predicate so ON CONFLICT matches the index --
                # same pattern as `live_activities_v3.register_live_activity`.
                index_where=PushJob.status.in_(PUSH_JOB_DEDUPE_ACTIVE_STATUSES),
                set_={
                    "fire_at": now,
                    "status": PushJobStatus.pending.value,
                    "updated_at": now,
                },
                # A job that already fired (sent/partial_failed) is left
                # alone rather than resurrected -- only accelerate one
                # still waiting to go out. `payload` is deliberately absent
                # from this SET: the client's original snapshot survives.
                where=PushJob.status.in_(
                    [PushJobStatus.pending.value, PushJobStatus.processing.value]
                ),
            )
        )
        await session.execute(stmt)
        if counts_as_created:
            created += 1

    return created
