"""The silent push that asks a user's devices to sync after the server
changed their data: the sync executor's writes, an assignment found
submitted by the push pipeline, and an edit one device made that the
others should fetch."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, exists, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.models import (
    PUSH_JOB_DEDUPE_ACTIVE_WHERE,
    PushDelivery,
    PushJob,
    PushJobStatus,
)

# Apple advises no more than two or three background pushes an hour
# (https://developer.apple.com/documentation/usernotifications/pushing-background-updates-to-your-app).
MIN_GAP = timedelta(minutes=20)

_MAX_ROUNDS = 5


async def enqueue_sync_trigger(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    source_device_id: str | None = None,
) -> int | None:
    """Tell the user's devices to sync, at most once per `MIN_GAP` (#107).

    A change made while a trigger has yet to go out rides on it. One made
    after, or while one is going out, gets a trigger `MIN_GAP` after the
    last. `source_device_id`, the device that made the change, is left out.
    Returns the id of the trigger filed, or None when none was.
    """
    latest = (
        await session.execute(
            _active(user_id)
            .order_by(PushJob.fire_at.desc(), PushJob.id.desc())
            .limit(1)
        )
    ).first()
    # Bounded: each round moves to a newer trigger another writer filed.
    for _ in range(_MAX_ROUNDS):
        if latest is not None and await _ride(session, latest.id, source_device_id):
            return None
        now = datetime.now(UTC)
        key = f"sync_trigger:{user_id}:after:{latest.id if latest else 0}"
        filed = await session.scalar(
            pg_insert(PushJob)
            .values(
                user_id=user_id,
                # Writers that read the same latest trigger file one.
                dedupe_key=key,
                channel="system",
                scenario="sync_trigger",
                fire_at=now if latest is None else max(now, latest.fire_at + MIN_GAP),
                payload={"kind": "sync_trigger", "source_device_id": source_device_id},
            )
            .on_conflict_do_nothing()
            .returning(PushJob.id)
        )
        if filed is not None:
            return filed
        # Another writer filed it first: ride on that one, or file after it.
        latest = (
            await session.execute(_active(user_id).where(PushJob.dedupe_key == key))
        ).first() or latest
    return None


def _active(user_id: uuid.UUID) -> Select:
    """The user's triggers that are waiting, going out or sent."""
    return select(PushJob.id, PushJob.status, PushJob.fire_at).where(
        PushJob.user_id == user_id,
        PushJob.scenario == "sync_trigger",
        PUSH_JOB_DEDUPE_ACTIVE_WHERE,
    )


async def _ride(
    session: AsyncSession, job_id: int, source_device_id: str | None
) -> bool:
    """Put this change on trigger `job_id` if it has yet to go out. Locked until
    this commits, the claim skips it, so it goes out after the change; one a
    claim holds already is skipped."""
    waiting = (
        await session.execute(
            select(PushJob)
            .where(
                PushJob.id == job_id,
                PushJob.status == PushJobStatus.pending.value,
                ~exists().where(PushDelivery.push_job_id == PushJob.id),
            )
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if waiting is None:
        return False
    payload = waiting.payload or {}
    if payload.get("source_device_id") != source_device_id:
        waiting.payload = {**payload, "source_device_id": None}
    return True
