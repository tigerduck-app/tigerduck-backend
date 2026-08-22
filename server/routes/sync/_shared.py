"""Cross-cutting helpers for the sync endpoints: nudging the push tick,
clearing queued deliveries for a device that just re-synced, and pushing
back queued sync jobs so a client that has just uploaded is not
immediately told to sync again."""

from __future__ import annotations
from datetime import UTC, datetime, timedelta
import asyncio
import structlog
from fastapi import APIRouter, BackgroundTasks, Query, Request
from sqlalchemy import delete, func, select, text, update
from server.auth.models import PushDelivery, PushDeliveryStatus, PushJob, PushJobStatus, User, UserDevice
from server.syncjobs.models import SyncJob, SyncJobStatus


logger = structlog.get_logger(__name__)
MAX_SYNC_LIMIT = 500
_CLIENT_SYNC_PUSHBACK_SECONDS = 7200  # 2 hours
_push_tick_lock = asyncio.Lock()
async def _trigger_push_tick(request: Request) -> None:
    """Run the push pipeline tick immediately so sync_trigger pushes
    are delivered within seconds instead of waiting up to 30s."""
    worker = getattr(request.app.state, "push_worker", None)
    if worker is None:
        return
    if _push_tick_lock.locked():
        return
    async with _push_tick_lock:
        from server.push.pipeline import run_push_tick
        await run_push_tick(worker)
async def _cancel_pending_deliveries_for_device(session, user_id, device_id) -> None:
    """Cancel ALL pending pushes for *device_id*.

    When the device just did a full sync it already has fresh data — no
    need for sync_trigger, schedule, or reminder pushes.

    Two passes:
    1. Device-targeted jobs (schedule pushes with job.device_id set):
       cancel the entire job since it only targets this device.
    2. User-scoped jobs (sync_trigger etc. with device_id NULL):
       skip only this device's materialized deliveries so other devices
       still receive the push.
    """
    now = datetime.now(UTC)
    device_row = (await session.execute(
        select(UserDevice.id).where(
            UserDevice.id == device_id,
            UserDevice.user_id == user_id,
            UserDevice.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    if device_row is None:
        return

    cancelled_jobs = await session.execute(
        update(PushJob)
        .where(
            PushJob.user_id == user_id,
            PushJob.device_id == device_row,
            PushJob.status == PushJobStatus.pending.value,
        )
        .values(status=PushJobStatus.cancelled.value, cancelled_at=now)
    )
    job_count = cancelled_jobs.rowcount or 0

    pending_job_ids = (await session.execute(
        select(PushJob.id).where(
            PushJob.user_id == user_id,
            PushJob.device_id.is_(None),
            PushJob.status.in_([PushJobStatus.pending.value, PushJobStatus.processing.value]),
        )
    )).scalars().all()
    delivery_count = 0
    if pending_job_ids:
        result = await session.execute(
            update(PushDelivery)
            .where(
                PushDelivery.push_job_id.in_(pending_job_ids),
                PushDelivery.device_id == device_row,
                PushDelivery.status == PushDeliveryStatus.pending.value,
            )
            .values(
                status=PushDeliveryStatus.skipped.value,
                failure_code="device_already_synced",
            )
        )
        delivery_count = result.rowcount or 0

    if job_count or delivery_count:
        logger.info(
            "sync.cancelled_pushes_for_device",
            user_id=str(user_id),
            device_id=str(device_id),
            jobs_cancelled=job_count,
            deliveries_skipped=delivery_count,
        )
async def _push_back_sync_jobs(session, user_id, *, seconds: int = _CLIENT_SYNC_PUSHBACK_SECONDS) -> None:
    """Push all pending sync jobs for *user_id* to ``now + seconds``.

    Called after a client sync or upload so the server's own sync cycle
    defers — the client just delivered fresh data, no need for the server
    to re-fetch from Moodle immediately.
    """
    run_after = datetime.now(UTC) + timedelta(seconds=seconds)
    await session.execute(
        update(SyncJob)
        .where(
            SyncJob.user_id == user_id,
            SyncJob.status == SyncJobStatus.pending.value,
        )
        .values(run_after=run_after)
    )
