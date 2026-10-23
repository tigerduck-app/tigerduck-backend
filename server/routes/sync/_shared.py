"""Cross-cutting helpers for the sync endpoints: nudging the push tick,
clearing queued deliveries for a device that just re-synced, and pushing
back queued sync jobs so a client that has just uploaded is not
immediately told to sync again."""

from __future__ import annotations
from datetime import UTC, datetime, timedelta
import asyncio
import structlog
from fastapi import APIRouter, BackgroundTasks, Query, Request
from sqlalchemy import delete, func, literal_column, select, text, update
from server.auth.models import PushDelivery, PushDeliveryStatus, PushJob, PushJobStatus, User, UserDevice
from server.syncjobs.models import SyncJob, SyncJobStatus, SyncPolicy


logger = structlog.get_logger(__name__)
MAX_SYNC_LIMIT = 500
_CLIENT_SYNC_PUSHBACK_SECONDS = 7200  # 2 hours
_push_tick_lock = asyncio.Lock()
async def _trigger_push_tick(request: Request) -> None:
    """Run the push pipeline tick immediately so sync_trigger pushes
    are delivered at once instead of at the next scheduled tick."""
    worker = getattr(request.app.state, "push_worker", None)
    if worker is None:
        return
    if _push_tick_lock.locked():
        return
    async with _push_tick_lock:
        from server.push.pipeline import run_push_tick
        await run_push_tick(worker)
async def _cancel_pending_deliveries_for_device(session, user_id, device_id) -> None:
    """Skip this device's queued deliveries of account-wide pushes.

    A device that has just full-synced holds fresh data, so its copy of an
    account-wide push -- a job with no `device_id`, such as a sync trigger
    -- has nothing left to ask for. Only this device's delivery rows are
    skipped; every other device still receives the push.

    Jobs addressed to this one device are left alone. Each is a Live
    Activity start (`/schedule/sync`), a Live Activity end
    (`/live-activities/register`, `submission_cancel`) or an operator's
    push, and a full sync stands in for none of them. This used to cancel
    them: ends first, so an activity was never dismissed, and then starts,
    so a refresh whose full sync finished after its schedule sync -- or an
    app closed mid-refresh -- left no start job, and the next class began
    with no Live Activity. A start that is no longer wanted is replaced by
    the next `/schedule/sync`, and the pipeline drops one whose activity
    the app has already started itself.
    """
    device_row = (await session.execute(
        select(UserDevice.id).where(
            UserDevice.id == device_id,
            UserDevice.user_id == user_id,
            UserDevice.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    if device_row is None:
        return

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

    if delivery_count:
        logger.info(
            "sync.cancelled_pushes_for_device",
            user_id=str(user_id),
            device_id=str(device_id),
            deliveries_skipped=delivery_count,
        )
async def _push_back_sync_jobs(
    session, user_id, job_type: str, *, seconds: int = _CLIENT_SYNC_PUSHBACK_SECONDS
) -> None:
    """Hold off the server's own `job_type` sync for *user_id* until
    ``now + seconds``: the client just uploaded what it would fetch.

    Never pulls a run forward, and leaves a job alone once a policy interval
    has passed since its last success, so a client uploading every few
    minutes delays the server's run by at most `seconds` past its schedule
    (tigerduck-backend#24).
    """
    now = datetime.now(UTC)
    interval = literal_column("interval '1 second'") * SyncPolicy.default_interval_seconds
    await session.execute(
        update(SyncJob)
        .where(
            SyncJob.user_id == user_id,
            SyncJob.job_type == job_type,
            SyncJob.status == SyncJobStatus.pending.value,
            SyncPolicy.job_type == SyncJob.job_type,
            SyncJob.last_success_at + interval > now,
        )
        .values(
            run_after=func.greatest(SyncJob.run_after, now + timedelta(seconds=seconds))
        )
    )
