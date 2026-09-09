"""v3 schedule sync — JWT-authenticated, creates PushJobs."""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from server.auth.dependencies import CurrentAuthDep
from server.auth.models import PushJob, PushJobStatus
from server.auth.schemas import ScheduleSyncV3Request, ScheduleSyncV3Response
from server.db import SessionDep
from server.push.dedupe import schedule_key, schedule_prefix

router = APIRouter(prefix="/schedule", tags=["schedule"])
logger = structlog.get_logger(__name__)

CHANNEL = "schedule"


def _activity_id(source_id: str, scenario: str) -> str:
    return f"{scenario}::{source_id}"


@router.post("/sync", response_model=ScheduleSyncV3Response)
async def sync_schedule(
    payload: ScheduleSyncV3Request,
    auth: CurrentAuthDep,
    session: SessionDep,
):
    if auth.device_id is None:
        # A schedule is per device — its push-to-start token, its end jobs
        # — and `/live-activities/register` already refuses a session
        # without one, so a start it filed could never be ended.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="device_id_required"
        )
    now = datetime.now(UTC)
    incoming_keys = {
        schedule_key(auth.device_id, e.source_id, e.scenario.value)
        for e in payload.events
    }

    # Only this device's own schedule jobs are up for replacement. The
    # channel is shared with the `la_end:{activity_id}` jobs that
    # `/live-activities/register` files for a running activity's end push;
    # sweeping the whole channel cancelled those on every sync, so the
    # activity never received its end and the pipeline's already-running
    # check could not see it either.
    existing = (
        (
            await session.execute(
                select(PushJob)
                .where(
                    PushJob.user_id == auth.user_id,
                    PushJob.channel == CHANNEL,
                    PushJob.status == PushJobStatus.pending.value,
                    PushJob.dedupe_key.startswith(
                        schedule_prefix(auth.device_id), autoescape=True
                    ),
                )
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    replaced = 0
    surviving_keys: set[str] = set()
    for job in existing:
        if job.dedupe_key not in incoming_keys:
            job.status = PushJobStatus.cancelled.value
            job.cancelled_at = now
            replaced += 1
        else:
            surviving_keys.add(job.dedupe_key)

    # Insert new events (dedupe via unique index — already-existing
    # keys are silently skipped).
    values = []
    for event in payload.events:
        key = schedule_key(auth.device_id, event.source_id, event.scenario.value)
        if key in surviving_keys:
            continue
        if event.fire_at <= now:
            continue
        values.append(
            {
                "user_id": auth.user_id,
                "device_id": auth.device_id,
                "dedupe_key": key,
                "channel": CHANNEL,
                "scenario": event.scenario.value,
                "fire_at": event.fire_at,
                # The snapshot is spread first: the routing keys are ours,
                # and a snapshot that happened to carry one must not
                # redirect the job.
                "payload": {
                    **event.snapshot,
                    "kind": "schedule",
                    "scenario": event.scenario.value,
                    "source_id": event.source_id,
                    # The id the started activity carries in its attributes,
                    # and so the key its update token is filed under when
                    # the client registers it — see `composedActivityId` on
                    # the client's LiveActivitySnapshot, which is
                    # "{scenario}::{sourceId}". Composed here rather than
                    # sent by the client so the end job's dedupe key and the
                    # update token's scope always agree with it.
                    "activity_id": _activity_id(event.source_id, event.scenario.value),
                },
            }
        )
    created = 0
    if values:
        result = await session.execute(
            pg_insert(PushJob).values(values).on_conflict_do_nothing()
        )
        created = result.rowcount or 0

    pending = len(surviving_keys) + created
    return ScheduleSyncV3Response(pending=pending, replaced=replaced)


@router.delete(
    "/{source_id}", status_code=status.HTTP_200_OK
)
async def cancel_schedule(
    source_id: str,
    auth: CurrentAuthDep,
    session: SessionDep,
):
    now = datetime.now(UTC)
    prefix = f"{schedule_prefix(auth.device_id)}{source_id}:"
    jobs = (
        (
            await session.execute(
                select(PushJob)
                .where(
                    PushJob.user_id == auth.user_id,
                    PushJob.channel == CHANNEL,
                    PushJob.dedupe_key.startswith(prefix, autoescape=True),
                    PushJob.status == PushJobStatus.pending.value,
                )
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    for job in jobs:
        job.status = PushJobStatus.cancelled.value
        job.cancelled_at = now
    return {"cancelled": len(jobs)}
