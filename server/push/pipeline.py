"""Two-phase user push pipeline (sync-and-push spec §5).

Claim/execute split mirrors `server/syncjobs/executor.py`: stale
`processing` rows are recovered first (attempts++, backoff — data-model
§2), then a short transaction claims due jobs under an advisory lock +
FOR UPDATE SKIP LOCKED, then each job runs in its own transaction:

  materialize: one push_deliveries row per active token
               (channel-aware: standard tokens for regular pushes; on the
               schedule channel a start goes to the device's push_to_start
               token and an end to the activity's own live_activity_update
               token; idempotent via ux_push_delivery_job_token DO NOTHING)
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
    PushTokenKind,
    PushTokenStatus,
    UserDevice,
)
from server.config import Settings
from server.db import session_scope
from server.push.client_versions import schedules_reminders_locally
from server.push.dedupe import activity_end_key
from server.push.job_payloads import build_apns_for_job, build_fcm_for_job
from server.push.reminders import CHANNEL as REMINDER_CHANNEL
from server.push.router import PushRouter
# Not a cycle: `syncjobs.credentials` imports only auth/i18n/syncjobs models,
# never `server.push`. Kept as a module-level import (unlike the deferred
# `log_entries` import below) so an accidental future cycle fails loudly at
# startup rather than on the first reauth delivery.
from server.syncjobs.credentials import REAUTH_SCENARIO, build_reauth_payload

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


def _has_copy(payload: dict) -> bool:
    """Whether an alert payload carries text a person could actually read.

    Only asked of server-composed alert copy. Background and Live Activity
    payloads legitimately have no title/body — a `sync_trigger` is silent by
    design and a schedule push takes its text from the snapshot — so this is
    never a blanket precondition of `_send_one`.
    """
    return bool(
        str(payload.get("title") or "").strip()
        and str(payload.get("body") or "").strip()
    )


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
            if await _materialize(session, job):
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


async def _materialize(session: AsyncSession, job: PushJob) -> bool:
    """Create one delivery row per active token. Idempotent — a stale-
    recovered job re-materializes onto the same unique index.

    Channel-aware: regular pushes go to standard tokens. On the schedule
    channel the job's `kind` decides — a start (`schedule`, written by
    `/schedule/sync`) goes to the device's push-to-start token, an end
    (`live_activity_end`, written by `/live-activities/register`) to the
    running activity's own update token. Neither token can do the other's
    job: APNs accepts an "end" sent to a push-to-start token and leaves the
    running activity untouched, which is what once kept the Dynamic Island
    up after class; and an update token can only address an activity that
    already exists.

    Returns False when the job was settled here and must not be delivered.

    Skips macOS devices (foreground-only, no background push). Non-macOS
    devices always receive the push — collapse keys deduplicate on the
    device side, and foreground devices simply ignore redundant syncs.
    """

    now = datetime.now(UTC)
    payload = job.payload or {}
    is_activity_job = job.channel == "schedule"
    is_activity_end = is_activity_job and payload.get("kind") == "live_activity_end"
    if is_activity_end:
        target_token_kind = "live_activity_update"
    elif is_activity_job:
        target_token_kind = "push_to_start"
    else:
        target_token_kind = "standard"
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
        # Both kinds need the activity id. For an end it picks the token:
        # `scope_key` is the activity id the register endpoint filed the
        # token under, and a device running two activities holds two update
        # tokens, so an unscoped query would end both from the one job. For
        # a start it is the attribute the payload creates the activity with,
        # and the key the client will register the update token under. No
        # activity id means we cannot tell activities apart, so send nothing
        # and let the job fail loudly rather than end or start the wrong one.
        activity_id = payload.get("activity_id")
        if not activity_id:
            logger.warning("push.activity_job_without_activity_id", job_id=job.id)
            _settle(job, status=PushJobStatus.failed, error="missing_activity_id", now=now)
            return False
        if is_activity_end:
            token_query = token_query.where(DevicePushToken.scope_key == activity_id)
        elif not await _has_deliveries(session, job) and (
            await _activity_already_registered(
                session, job=job, activity_id=activity_id, now=now
            )
        ):
            # The app starts this same activity itself when it is in the
            # foreground at fire time, and registers the update token as it
            # does. A start push on top of that puts a second copy on the
            # lock screen and rings the alert for something already showing.
            # Settled as cancelled, not failed: nothing went wrong, the
            # activity simply did not need us.
            #
            # Only decided before the first delivery row exists. This runs
            # again on every retry round, and by then the push may already
            # have reached one of the job's tokens and started the very
            # activity the registration now describes — cancelling here
            # would strand the other deliveries as pending and record a
            # sent push as never sent.
            _settle(
                job,
                status=PushJobStatus.cancelled,
                error="activity_already_running",
                now=now,
            )
            logger.info(
                "push.activity_already_running", job_id=job.id, activity_id=activity_id
            )
            return False

    is_sync_trigger = job.scenario == "sync_trigger"
    source_device_id: str | None = None
    if is_sync_trigger:
        token_query = token_query.where(UserDevice.cloud_sync_enabled.is_(True))
        source_device_id = (job.payload or {}).get("source_device_id")

    is_assignment_reminder = job.channel == REMINDER_CHANNEL
    if is_assignment_reminder:
        # iPhone and iPad only. Android schedules these locally (spec 4.2)
        # and macOS takes no notifications at all, so a delivery row for
        # either is a duplicate at best. Both device switches must be on:
        # the parent one because a user who turned sync off has no
        # expectation of server-driven reminders, and the child one
        # because that is the row the settings screen actually renders.
        token_query = token_query.where(
            UserDevice.cloud_sync_enabled.is_(True),
            UserDevice.sync_assignment_reminders.is_(True),
            UserDevice.platform.in_(("ios", "ipados")),
        )

    rows = (await session.execute(token_query)).all()
    if not rows:
        return True
    values = []
    for token, device in rows:
        if device.platform == "macos":
            continue
        if is_assignment_reminder and schedules_reminders_locally(
            device.platform, device.app_version
        ):
            # A client that still has its own scheduler would show this
            # reminder twice. Version comparison cannot go in the query
            # above: app_version is VARCHAR, and "2.10.0" sorts below
            # "2.9.0" as a string.
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
        return True
    await session.execute(
        pg_insert(PushDelivery)
        .values(values)
        .on_conflict_do_nothing(
            index_elements=["push_job_id", "token_hash", "token_kind", "scope_key"]
        )
    )
    return True


def _settle(job: PushJob, *, status: PushJobStatus, error: str, now: datetime) -> None:
    """Finish `job` here, without a delivery round."""
    job.status = status.value
    job.last_error = error
    if status is PushJobStatus.cancelled:
        job.cancelled_at = now
    job.locked_by = None
    job.locked_at = None


async def _has_deliveries(session: AsyncSession, job: PushJob) -> bool:
    row = (
        await session.execute(
            select(PushDelivery.id).where(PushDelivery.push_job_id == job.id).limit(1)
        )
    ).first()
    return row is not None


async def _activity_already_registered(
    session: AsyncSession, *, job: PushJob, activity_id: str, now: datetime
) -> bool:
    """Whether the device this start job addresses has already registered
    `activity_id` and its countdown has not passed.

    `/live-activities/register` files the activity's end job under the
    device's `la_end` key at its countdown target; while that job is still
    waiting to fire, the activity is on screen. Only a pending job can be
    waiting: the claim only takes jobs whose `fire_at` has passed, and
    re-registering with a later target puts the job back to pending. The
    token rows cannot say this — an update token stays active after its
    activity ends until something is pushed to it and APNs answers 410.

    Every schedule job carries a device: `/schedule/sync` refuses a
    session without one, as `/live-activities/register` does.
    """
    row = (
        await session.execute(
            select(PushJob.id)
            .where(
                PushJob.user_id == job.user_id,
                PushJob.dedupe_key == activity_end_key(job.device_id, activity_id),
                PushJob.status == PushJobStatus.pending.value,
                PushJob.fire_at > now,
            )
            .limit(1)
        )
    ).first()
    return row is not None


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

    payload = job.payload or {}
    if job.scenario == REAUTH_SCENARIO:
        # Keyed on the job's scenario, not payload["kind"]: the portal's
        # operator "retry + notify" endpoints (portal/app/routes/moodle/
        # jobs.py) insert reauth_required jobs directly via SQL in the
        # pre-v2.1.0 payload shape (no `kind`), and always will — the portal
        # is a separate package that talks raw SQL and does not import this
        # module. Keying on `kind` let those jobs skip copy rebuild entirely
        # and ship the empty-banner bug this branch exists to fix.
        #
        # Copy is resolved here, not at enqueue time: one job fans out to
        # every device on the account and they can be in different
        # languages, so the only place the right language is known is the
        # recipient. `session.get` is an identity-map hit — `_materialize`
        # loaded this device earlier in the same transaction.
        device = await session.get(UserDevice, delivery.device_id)
        payload = build_reauth_payload(
            provider=payload.get("provider", ""),
            locale=device.locale if device is not None else None,
        )
        if not _has_copy(payload):
            # Sending this would be worse than not sending it. APNs coerces a
            # missing title to "" and delivers a banner that rings with no
            # text; Android's FcmService drops a `reauth_required` with no
            # copy and logs a warning nobody reads. Either way the user is
            # told nothing, so the failure has to surface here instead —
            # skipped, not failed, because nothing was attempted and a retry
            # would resolve the same empty string (same shape as `token_gone`
            # above). `MISSING_KEY_SENTINEL` deliberately does NOT land here:
            # `server/i18n.py` returns a visibly broken string precisely so a
            # missing key becomes a bug report rather than silence.
            delivery.status = PushDeliveryStatus.skipped.value
            delivery.failure_code = "empty_copy"
            logger.error(
                "push.empty_copy",
                job_id=job.id,
                delivery_id=delivery.id,
                device_id=str(delivery.device_id),
                locale=device.locale if device is not None else None,
            )
            return

    delivery.attempts += 1

    if delivery.provider == "apns":
        apns_request = build_apns_for_job(
            payload=payload,
            channel=job.channel,
            token_value=token.token_value,
            bundle_id=token.bundle_id or worker.settings.apns_bundle_id,
            now=now,
            # A push-to-start token is registered under the ActivityAttributes
            # type name it starts; the start payload has to name that type.
            attributes_type=(
                (token.scope_key or None)
                if token.token_kind == PushTokenKind.push_to_start.value
                else None
            ),
        )
        result = await worker.router.send_apple(apns_request)
    else:
        fcm_request = build_fcm_for_job(
            payload=payload, channel=job.channel, token_value=token.token_value
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
