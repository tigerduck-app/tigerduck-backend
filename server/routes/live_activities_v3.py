"""v3 Live Activity update token registration.

Upserts a DevicePushToken with kind=live_activity_update and schedules
an "end" PushJob at countdown_target.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.dependencies import CurrentAuthDep
from server.auth.models import (
    PUSH_JOB_DEDUPE_ACTIVE_STATUSES,
    DevicePushToken,
    PushJob,
    PushJobStatus,
    PushTokenStatus,
)
from server.auth.schemas import (
    LiveActivityRegisterV3Request,
    LiveActivityRegisterV3Response,
)
from server.db import SessionDep
from server.push.dedupe import SCHEDULE_CHANNEL, activity_end_key

router = APIRouter(prefix="/live-activities", tags=["live-activities"])
logger = structlog.get_logger(__name__)


async def _retire_finished_end_job(
    session: AsyncSession, *, user_id: uuid.UUID, dedupe_key: str
) -> None:
    """Move an end job that already went out off `dedupe_key`.

    `ux_push_jobs_dedupe_active` covers `sent` and `partial_failed` as well
    as jobs still waiting, so the end an earlier run of the same activity id
    received holds the key for good. The upsert in the route then conflicts
    with it, its update only applies to a job still waiting, and the
    activity registering now got no end job at all: it stayed on screen
    past its countdown until iOS's own cleanup. The id repeats whenever a
    class occurrence runs twice (the debug clock does exactly that) and
    whenever an assignment gets a second activity.

    A register whose countdown is still ahead is a running activity, so
    whatever that earlier end did is over. The row keeps its status and
    history under a key of its own, and the exact key is left for the new
    job: the pipeline's already-running check and `submission_cancel` both
    look the end job up by it.
    """
    finished = (
        (
            await session.execute(
                select(PushJob)
                .where(
                    PushJob.user_id == user_id,
                    PushJob.dedupe_key == dedupe_key,
                    PushJob.status.in_(
                        [PushJobStatus.sent.value, PushJobStatus.partial_failed.value]
                    ),
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for job in finished:
        job.dedupe_key = f"{dedupe_key}:finished:{job.id}"
    if finished:
        # Before the upsert reads the index.
        await session.flush()


@router.post("/register", response_model=LiveActivityRegisterV3Response)
async def register_live_activity(
    payload: LiveActivityRegisterV3Request,
    auth: CurrentAuthDep,
    session: SessionDep,
):
    if auth.device_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="device_id_required",
        )

    # The id has to be the one `/schedule/sync` composes for a start —
    # "{scenario}::{source_id}" — or a push-started activity and the one the
    # client registers are two different things to the pipeline: the end
    # job addresses one and the already-running check looks for the other.
    scenario = payload.snapshot.get("scenario")
    if scenario is not None and payload.activity_id != f"{scenario}::{payload.source_id}":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="activity_id_mismatch",
        )

    token_hash = hashlib.sha256(payload.update_token_hex.encode()).hexdigest()
    now = datetime.now(UTC)

    # Invalidate any prior active token for this activity on this device.
    old_tokens = (
        (
            await session.execute(
                select(DevicePushToken).where(
                    DevicePushToken.device_id == auth.device_id,
                    DevicePushToken.token_kind == "live_activity_update",
                    DevicePushToken.scope_key == payload.activity_id,
                    DevicePushToken.status == PushTokenStatus.active.value,
                )
            )
        )
        .scalars()
        .all()
    )
    for old in old_tokens:
        if old.token_hash != token_hash:
            old.status = PushTokenStatus.invalidated.value

    # Upsert the new token.
    stmt = (
        pg_insert(DevicePushToken)
        .values(
            device_id=auth.device_id,
            provider="apns",
            token_kind="live_activity_update",
            token_hash=token_hash,
            token_value=payload.update_token_hex,
            bundle_id=payload.bundle_id,
            topic=f"{payload.bundle_id}.push-type.liveactivity",
            environment=payload.environment,
            scope_key=payload.activity_id,
            status=PushTokenStatus.active.value,
        )
        .on_conflict_do_update(
            index_elements=["provider", "token_kind", "token_hash", "scope_key"],
            # ux_push_token_active is a PARTIAL unique index (WHERE
            # status='active'); the predicate must be repeated here or
            # Postgres can't match the ON CONFLICT target.
            index_where=DevicePushToken.status == PushTokenStatus.active.value,
            set_={
                "device_id": auth.device_id,
                "token_value": payload.update_token_hex,
                "status": PushTokenStatus.active.value,
                "updated_at": now,
            },
            where=DevicePushToken.status == PushTokenStatus.active.value,
        )
        .returning(DevicePushToken.id)
    )
    result = await session.execute(stmt)
    token_id = result.scalar_one()

    # Schedule an "end" push at countdown_target.
    dedupe_key = activity_end_key(auth.device_id, payload.activity_id)
    end_job_id = None
    if payload.countdown_target > now:
        await _retire_finished_end_job(
            session, user_id=auth.user_id, dedupe_key=dedupe_key
        )
        end_stmt = (
            pg_insert(PushJob)
            .values(
                user_id=auth.user_id,
                device_id=auth.device_id,
                dedupe_key=dedupe_key,
                channel=SCHEDULE_CHANNEL,
                scenario="activityEnd",
                fire_at=payload.countdown_target,
                # Snapshot first, routing keys after: a snapshot carrying
                # one of our keys must not redirect the job.
                payload={
                    **payload.snapshot,
                    "kind": "live_activity_end",
                    "activity_id": payload.activity_id,
                    "source_id": payload.source_id,
                },
            )
            .on_conflict_do_update(
                index_elements=["user_id", "dedupe_key"],
                # ux_push_jobs_dedupe_active is partial (active statuses);
                # repeat its predicate so ON CONFLICT matches the index.
                index_where=PushJob.status.in_(PUSH_JOB_DEDUPE_ACTIVE_STATUSES),
                set_={
                    "fire_at": payload.countdown_target,
                    "payload": {
                        **payload.snapshot,
                        "kind": "live_activity_end",
                        "activity_id": payload.activity_id,
                        "source_id": payload.source_id,
                    },
                    "status": PushJobStatus.pending.value,
                    "updated_at": now,
                },
                where=PushJob.status.in_(
                    [PushJobStatus.pending.value, PushJobStatus.processing.value]
                ),
            )
            .returning(PushJob.id)
        )
        result = await session.execute(end_stmt)
        row = result.first()
        if row:
            end_job_id = row[0]
        else:
            # The activity is running with nothing scheduled to end it.
            # Not expected once finished jobs are retired above, so leave a
            # trace rather than answer as if it will be ended.
            logger.warning(
                "live_activities.end_job_not_filed",
                activity_id=payload.activity_id,
            )

    return LiveActivityRegisterV3Response(token_id=token_id, end_job_id=end_job_id)
