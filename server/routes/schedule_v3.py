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

router = APIRouter(prefix="/schedule", tags=["schedule"])
logger = structlog.get_logger(__name__)

CHANNEL = "schedule"


def _dedupe_key(source_id: str, scenario: str) -> str:
    return f"schedule:{source_id}:{scenario}"


@router.post("/sync", response_model=ScheduleSyncV3Response)
async def sync_schedule(
    payload: ScheduleSyncV3Request,
    auth: CurrentAuthDep,
    session: SessionDep,
):
    now = datetime.now(UTC)
    incoming_keys = {
        _dedupe_key(e.source_id, e.scenario.value) for e in payload.events
    }

    # Cancel pending schedule jobs whose key is no longer in the upload.
    existing = (
        (
            await session.execute(
                select(PushJob)
                .where(
                    PushJob.user_id == auth.user_id,
                    PushJob.channel == CHANNEL,
                    PushJob.status == PushJobStatus.pending.value,
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
        key = _dedupe_key(event.source_id, event.scenario.value)
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
                "payload": {
                    "kind": "schedule",
                    "scenario": event.scenario.value,
                    "source_id": event.source_id,
                    **event.snapshot,
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
    jobs = (
        (
            await session.execute(
                select(PushJob)
                .where(
                    PushJob.user_id == auth.user_id,
                    PushJob.channel == CHANNEL,
                    PushJob.dedupe_key.like(f"schedule:{source_id}:%"),
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
