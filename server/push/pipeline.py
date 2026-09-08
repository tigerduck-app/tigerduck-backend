"""Two-phase user push pipeline (sync-and-push spec §5).

Claim/execute split mirrors `server/syncjobs/executor.py`: stale
`processing` rows are recovered first (attempts++, backoff — data-model
§2), then a short transaction claims due jobs under an advisory lock +
FOR UPDATE SKIP LOCKED, then each job runs in its own transaction:

  materialize: one push_deliveries row per active token
               (channel-aware: standard tokens for regular pushes, push_to_start
               tokens for schedule; idempotent via ux_push_delivery_job_token DO NOTHING)
  deliver:     send each due pending delivery once via PushRouter;
               unregistered tokens are invalidated, transient failures
               keep the delivery pending with next_retry_at backoff
  aggregate:   ≥1 sent & no failed → sent; mix → partial_failed;
               all failed → failed; no tokens → failed/no_active_tokens.
               Still-pending deliveries push the JOB back to pending for
               another round until job.max_attempts rounds are spent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.auth.models import (
    DevicePushToken,
    PushDelivery,
    PushDeliveryStatus,
    PushJob,
    PushJobStatus,
    PushTokenStatus,
    UserDevice,
)
from server.config import Settings
from server.db import session_scope
from server.push.job_payloads import build_apns_for_job, build_fcm_for_job
from server.push.router import PushRouter

logger = structlog.get_logger(__name__)

# "TD_PUSH" — serializes the claim phase across workers.
_CLAIM_LOCK_KEY = 0x54445F50555348

_UNREGISTERED_APNS_DESCRIPTIONS = {"baddevicetoken", "unregistered"}


@dataclass(frozen=True)
class PushPipelineWorker:
    session_factory: async_sessionmaker[AsyncSession]
    settings: Settings
    router: PushRouter
    worker_id: str


def _is_unregistered(status: str | None, description: str | None) -> bool:
    s = (status or "").lower()
    d = (description or "").lower()
    return s in {"410", "unregistered"} or d in _UNREGISTERED_APNS_DESCRIPTIONS


async def run_push_tick(worker: PushPipelineWorker) -> int:
    """One scheduler tick: recover stale jobs, claim due jobs, process
    them sequentially. Returns the number of jobs processed."""
    await _recover_stale_jobs(worker)
    claimed = await _claim_due_jobs(worker)
    for job_id in claimed:
        await _process_job(worker, job_id=job_id)
    return len(claimed)


async def _recover_stale_jobs(worker: PushPipelineWorker) -> None:
    cutoff = datetime.now(UTC) - timedelta(
        minutes=worker.settings.push_job_stale_lock_minutes
    )
    async with session_scope(worker.session_factory) as session:
        jobs = (
            (
                await session.execute(
                    select(PushJob)
                    .where(
                        PushJob.status == PushJobStatus.processing.value,
                        PushJob.locked_at < cutoff,
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        if not jobs:
            return
        now = datetime.now(UTC)
        for job in jobs:
            job.locked_by = None
            job.locked_at = None
            job.attempts += 1
            if job.attempts >= job.max_attempts:
                job.status = PushJobStatus.failed.value
                job.last_error = "stale_lock"
            else:
                job.status = PushJobStatus.pending.value
                backoff = worker.settings.push_retry_round_delay_seconds * (
                    2 ** (job.attempts - 1)
                )
                job.available_at = now + timedelta(seconds=backoff)
        logger.warning("push.stale_recovered", count=len(jobs))


async def _claim_due_jobs(worker: PushPipelineWorker) -> list[int]:
    now = datetime.now(UTC)
    async with session_scope(worker.session_factory) as session:
        await session.execute(select(func.pg_advisory_xact_lock(_CLAIM_LOCK_KEY)))
        jobs = (
            (
                await session.execute(
                    select(PushJob)
                    .where(
                        PushJob.status == PushJobStatus.pending.value,
                        PushJob.fire_at <= now,
                        PushJob.available_at <= now,
                    )
                    .order_by(PushJob.priority, PushJob.fire_at)
                    .limit(worker.settings.push_pipeline_batch_size)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        for job in jobs:
            job.status = PushJobStatus.processing.value
            job.locked_by = worker.worker_id
            job.locked_at = now
        return [job.id for job in jobs]


async def _process_job(worker: PushPipelineWorker, *, job_id: int) -> None:
    try:
        async with session_scope(worker.session_factory) as session:
            job = (
                await session.execute(
                    select(PushJob).where(PushJob.id == job_id).with_for_update()
                )
            ).scalar_one()
            if (
                job.status != PushJobStatus.processing.value
                or job.locked_by != worker.worker_id
            ):
                # Stale-recovered (and possibly reclaimed) between our claim
                # commit and now — never double-process.
                logger.warning("push.job_reclaimed", job_id=job_id)
                return
            await _materialize(session, job)
            await _deliver_round(worker, session, job)
    except Exception:
        logger.exception("push.job_crashed", job_id=job_id)
        async with session_scope(worker.session_factory) as session:
            job = (
                await session.execute(
                    select(PushJob).where(PushJob.id == job_id).with_for_update()
                )
            ).scalar_one_or_none()
            if job is None or job.status != PushJobStatus.processing.value:
                return
            now = datetime.now(UTC)
            job.locked_by = None
            job.locked_at = None
            job.attempts += 1
            if job.attempts >= job.max_attempts:
                job.status = PushJobStatus.failed.value
                job.last_error = "pipeline_crash"
            else:
                job.status = PushJobStatus.pending.value
                job.available_at = now + timedelta(
                    seconds=worker.settings.push_retry_round_delay_seconds
                )


async def _materialize(session: AsyncSession, job: PushJob) -> None:
    """Create one delivery row per active token (channel-aware: standard tokens
    for regular pushes, the activity's own update token for schedule).
    Idempotent — a stale-recovered job re-materializes onto the same unique
    index.

    Skips macOS devices (foreground-only, no background push). Non-macOS
    devices always receive the push — collapse keys deduplicate on the
    device side, and foreground devices simply ignore redundant syncs.
    """

    now = datetime.now(UTC)
    # A schedule job addresses one Live Activity that is already running, so
    # it needs that activity's update token. A push-to-start token can only
    # start an activity — APNs accepts an "update"/"end" sent to one and the
    # running activity is left untouched, which is what stopped the Dynamic
    # Island clearing at the end of a class.
    is_activity_job = job.channel == "schedule"
    target_token_kind = "live_activity_update" if is_activity_job else "standard"
    token_query = (
        select(DevicePushToken, UserDevice)
        .join(UserDevice, UserDevice.id == DevicePushToken.device_id)
        .where(
            UserDevice.user_id == job.user_id,
            UserDevice.deleted_at.is_(None),
            DevicePushToken.token_kind == target_token_kind,
            DevicePushToken.status == PushTokenStatus.active.value,
            (DevicePushToken.expires_at.is_(None))
            | (DevicePushToken.expires_at > now),
        )
    )
    if job.device_id is not None:
        token_query = token_query.where(UserDevice.id == job.device_id)

    if is_activity_job:
        # `scope_key` is the activity id the register endpoint filed the
        # token under. Narrowing by it is load-bearing: a device running
        # two activities holds two update tokens, and an unscoped query
        # would end both from the one job. No activity id means we cannot
        # tell them apart, so send nothing and let the job fail loudly
        # rather than dismiss the wrong activity.
        activity_id = (job.payload or {}).get("activity_id")
        if not activity_id:
            logger.warning("push.activity_job_without_activity_id", job_id=job.id)
            return
        token_query = token_query.where(DevicePushToken.scope_key == activity_id)

    is_sync_trigger = job.scenario == "sync_trigger"
    source_device_id: str | None = None
    if is_sync_trigger:
        token_query = token_query.where(UserDevice.cloud_sync_enabled.is_(True))
        source_device_id = (job.payload or {}).get("source_device_id")

    rows = (await session.execute(token_query)).all()
    if not rows:
        return
    values = []
    for token, device in rows:
        if device.platform == "macos":
            continue
        if is_sync_trigger and source_device_id and str(device.id) == source_device_id:
            continue
        values.append(
            {
                "push_job_id": job.id,
                "user_id": job.user_id,
                "device_id": token.device_id,
                "push_token_id": token.id,
                "provider": token.provider,
                "token_kind": token.token_kind,
                "token_hash": token.token_hash,
                "scope_key": token.scope_key,
            }
        )
    if not values:
        return
    await session.execute(
        pg_insert(PushDelivery)
        .values(values)
        .on_conflict_do_nothing(
            index_elements=["push_job_id", "token_hash", "token_kind", "scope_key"]
        )
    )


async def _deliver_round(
    worker: PushPipelineWorker, session: AsyncSession, job: PushJob
) -> None:
    now = datetime.now(UTC)
    deliveries = (
        (
            await session.execute(
                select(PushDelivery).where(PushDelivery.push_job_id == job.id)
            )
        )
        .scalars()
        .all()
    )
    if not deliveries:
        job.status = PushJobStatus.failed.value
        job.last_error = "no_active_tokens"
        job.locked_by = None
        job.locked_at = None
        logger.info("push.no_active_tokens", job_id=job.id)
        return

    attempted = 0
    for delivery in deliveries:
        if delivery.status != PushDeliveryStatus.pending.value:
            continue
        if delivery.next_retry_at > now:
            continue
        attempted += 1
        await _send_one(worker, session, job, delivery, now)

    statuses = [d.status for d in deliveries]
    pending = statuses.count(PushDeliveryStatus.pending.value)
    if pending:
        if attempted == 0 and job.attempts + 1 >= job.max_attempts:
            # Zero-work round (every pending delivery's next_retry_at was
            # still in the future — clock skew vs the round delay). Don't
            # let bookkeeping burn the job's LAST round and force-fail
            # deliveries that never used their attempts; just come back.
            job.status = PushJobStatus.pending.value
            job.available_at = now + timedelta(
                seconds=worker.settings.push_retry_round_delay_seconds
            )
            job.locked_by = None
            job.locked_at = None
            return
        if job.attempts + 1 < job.max_attempts:
            # Another round later — transient failures may clear up.
            job.attempts += 1
            job.status = PushJobStatus.pending.value
            job.available_at = now + timedelta(
                seconds=worker.settings.push_retry_round_delay_seconds
            )
            job.locked_by = None
            job.locked_at = None
            return
        for delivery in deliveries:
            if delivery.status == PushDeliveryStatus.pending.value:
                delivery.status = PushDeliveryStatus.failed.value
                # Keep the last real transport error if one was recorded.
                if delivery.failure_code is None:
                    delivery.failure_code = "retries_exhausted"
        statuses = [d.status for d in deliveries]

    sent = statuses.count(PushDeliveryStatus.sent.value)
    failed = statuses.count(PushDeliveryStatus.failed.value)
    if sent and not failed:
        # skipped deliveries don't demote a sent job (spec §5).
        job.status = PushJobStatus.sent.value
    elif sent:
        job.status = PushJobStatus.partial_failed.value
    else:
        job.status = PushJobStatus.failed.value
        job.last_error = (
            "all_tokens_skipped" if not failed else "all_deliveries_failed"
        )
    if sent:
        job.sent_at = now
    job.locked_by = None
    job.locked_at = None
    logger.info(
        "push.job_done", job_id=job.id, status=job.status, sent=sent, failed=failed
    )


async def _send_one(
    worker: PushPipelineWorker,
    session: AsyncSession,
    job: PushJob,
    delivery: PushDelivery,
    now: datetime,
) -> None:
    token = (
        await session.get(DevicePushToken, delivery.push_token_id)
        if delivery.push_token_id is not None
        else None
    )
    if token is None or token.status != PushTokenStatus.active.value:
        delivery.status = PushDeliveryStatus.skipped.value
        delivery.failure_code = "token_gone"
        return

    delivery.attempts += 1
    if delivery.provider == "apns":
        apns_request = build_apns_for_job(
            payload=job.payload,
            channel=job.channel,
            token_value=token.token_value,
            bundle_id=token.bundle_id or worker.settings.apns_bundle_id,
            now=now,
        )
        result = await worker.router.send_apple(apns_request)
    else:
        fcm_request = build_fcm_for_job(
            payload=job.payload, channel=job.channel, token_value=token.token_value
        )
        result = await worker.router.send_android(fcm_request)

    if result.success:
        delivery.status = PushDeliveryStatus.sent.value
        delivery.sent_at = now
        delivery.provider_message_id = result.notification_id
        token.last_success_at = now
        if job.scenario == "sync_trigger":
            await _log_sync_trigger_delivery(session, job, token, delivery.provider)
        return

    token.last_failure_at = now
    token.last_failure_code = result.status
    if _is_unregistered(result.status, result.description):
        delivery.status = PushDeliveryStatus.failed.value
        delivery.failure_code = "unregistered"
        delivery.failure_message = result.description
        token.status = PushTokenStatus.invalidated.value
        logger.info(
            "push.token_invalidated", token_id=token.id, status=result.status
        )
        return

    delivery.failure_code = result.status
    delivery.failure_message = result.description
    if delivery.attempts >= delivery.max_attempts:
        delivery.status = PushDeliveryStatus.failed.value
    else:
        # Flat delay aligned with the job's round delay — a growing
        # per-delivery backoff could outlive the job's remaining rounds
        # and force-fail a delivery that never used all its attempts.
        delivery.next_retry_at = now + timedelta(
            seconds=worker.settings.push_retry_round_delay_seconds
        )


async def _log_sync_trigger_delivery(
    session: AsyncSession, job: PushJob, token: DevicePushToken, provider: str
) -> None:
    try:
        from server.syncjobs.log_entries import log_sync

        device = await session.get(UserDevice, token.device_id)
        target_label = (
            f"{device.platform}/{device.client_device_id}"
            if device else f"token:{token.id}"
        )
        source_device_id = (job.payload or {}).get("source_device_id")
        await log_sync(
            session,
            user_id=job.user_id,
            source="push",
            message=f"sync_trigger sent via {provider} → {target_label}",
            device_id=source_device_id,
            detail={
                "target_device_id": str(token.device_id),
                "target_platform": device.platform if device else provider,
                "target_name": None,
                "provider": provider,
            },
        )
    except Exception:
        logger.debug("sync_trigger log write failed", exc_info=True)
