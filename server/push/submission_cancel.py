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

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.models import (
    DevicePushToken,
    PushJob,
    PushJobStatus,
    PushTokenStatus,
    UserDevice,
)
from server.auth.schemas import ScheduleScenario
from server.push.dedupe import activity_end_key
from server.push.reminders import CHANNEL as REMINDER_CHANNEL
from server.push.reminders import _dedupe_key
from server.routes.schedule_v3 import CHANNEL as ACTIVITY_CHANNEL
from server.routes.schedule_v3 import _activity_id

_LIVE_ACTIVITY_TOKEN_KIND = "live_activity_update"

# Matches `ux_push_jobs_dedupe_active`'s partial-index predicate -- the
# statuses under which a (user_id, dedupe_key) pair is still "taken".
_DEDUPE_ACTIVE_STATUSES = (
    PushJobStatus.pending.value,
    PushJobStatus.processing.value,
    PushJobStatus.sent.value,
    PushJobStatus.partial_failed.value,
)


def _reminder_key_prefix(moodle_assignment_id: int) -> str:
    """The portion of `_dedupe_key`'s output before offset/due_epoch, which
    are unknown at cancel time.

    Formats a placeholder key with `_dedupe_key` itself and truncates it,
    rather than retyping the literal `"assignment:moodle:{id}:reminder_"`
    format here -- a future change to that format then cannot silently
    desync the producer and this consumer.
    """
    probe = _dedupe_key(moodle_assignment_id, offset=0, due_epoch=0)
    marker = "reminder_"
    return probe[: probe.index(marker) + len(marker)]


def _activity_id_for_assignment(moodle_assignment_id: int) -> str:
    """The same `{scenario}::{source_id}` id the client composes for an
    assignment's Live Activity (`composedActivityId`) and the backend
    composes for a schedule-driven start (`schedule_v3._activity_id`).
    The assignment's source_id is its Moodle id as a string -- the client
    resolver sets `sourceId: assignment.assignmentId`, which is
    `String(record.assignId)`.
    """
    return _activity_id(
        str(moodle_assignment_id), ScheduleScenario.assignment_urgent.value
    )


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
    prefixes = [_reminder_key_prefix(aid) for aid in moodle_assignment_ids]
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
                DevicePushToken.token_kind == _LIVE_ACTIVITY_TOKEN_KIND,
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
                    PushJob.status.in_(_DEDUPE_ACTIVE_STATUSES),
                )
            )
        )
        .scalars()
        .all()
    )

    created = 0
    for device_id, activity_id, dedupe_key in targets:
        moodle_assignment_id = assignment_by_activity_id[activity_id]
        stmt = (
            pg_insert(PushJob)
            .values(
                user_id=user_id,
                device_id=device_id,
                dedupe_key=dedupe_key,
                channel=ACTIVITY_CHANNEL,
                scenario="activityEnd",
                fire_at=now,
                payload={
                    "kind": "live_activity_end",
                    "activity_id": activity_id,
                    "source_id": str(moodle_assignment_id),
                },
            )
            .on_conflict_do_update(
                index_elements=["user_id", "dedupe_key"],
                # ux_push_jobs_dedupe_active is partial (active statuses);
                # repeat its predicate so ON CONFLICT matches the index --
                # same pattern as `live_activities_v3.register_live_activity`.
                index_where=PushJob.status.in_(_DEDUPE_ACTIVE_STATUSES),
                set_={
                    "fire_at": now,
                    "status": PushJobStatus.pending.value,
                    "updated_at": now,
                },
                # A job that already fired (sent/partial_failed) is left
                # alone rather than resurrected -- only accelerate one
                # still waiting to go out.
                where=PushJob.status.in_(
                    [PushJobStatus.pending.value, PushJobStatus.processing.value]
                ),
            )
        )
        await session.execute(stmt)
        if dedupe_key not in existing_keys:
            created += 1

    return created
