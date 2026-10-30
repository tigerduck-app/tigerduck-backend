"""Two-phase user push pipeline (sync-and-push spec §5).

Claim/execute split mirrors `server/syncjobs/executor.py`: stale
`processing` rows are recovered first (attempts++, backoff — data-model
§2), then a short transaction claims due jobs under an advisory lock +
FOR UPDATE SKIP LOCKED, then each job runs in its own transaction:

  check:       an assignment reminder or countdown start is held back
               unless its assignment is confirmed still open
               (`push/presend.py`), asked before the transaction opens
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

import asyncio
import uuid

from collections.abc import Awaitable, Collection
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import (
    BigInteger,
    ColumnElement,
    all_,
    func,
    literal,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server import metrics
from server.academic_calendar.models import AcademicHoliday, UserHolidayOverride
from server.academic_calendar.terms import load_term_dates, outside_term
from server.auth.crypto import CredentialCipher
from server.auth.models import (
    APPLE_HANDHELD_PLATFORMS,
    PUSH_JOB_DEDUPE_ACTIVE_WHERE,
    DevicePushToken,
    PushDelivery,
    PushDeliveryStatus,
    PushJob,
    PushJobStatus,
    PushTokenKind,
    PushTokenStatus,
    UserDevice,
    UserDevicePlatform,
)
from server.auth.schemas import ScheduleScenario
from server.bulletins.user_dispatch import CHANNEL as BULLETIN_CHANNEL
from server.config import Settings
from server.db import lift_timeouts, session_scope
from server.push.apns_client import SendResult
from server.push.class_starts import recheck_class_start
from server.push.client_versions import schedules_reminders_locally
from server.push.course_reminders import CHANNEL as COURSE_CHANNEL
from server.push.course_reminders import recheck_class_reminder
from server.push.dedupe import SCHEDULE_CHANNEL, activity_end_key, schedule_key
from server.push.dedupe import activity_id as _activity_id
from server.push.fcm_client import is_dead_fcm_token
from server.push.job_payloads import build_apns_for_job, build_fcm_for_job
from server.push.notification_copy import (
    REAUTH_SCENARIO,
    SESSION_REAUTH_REASON,
    build_assignment_reminder_payload,
    build_course_reminder_payload,
    build_reauth_payload,
)
from server.push.payload import _to_unix_seconds
from server.push.presend import (
    MoodleBreaker,
    PresendCheck,
    _assignment_id,
    check_before_send,
    needs_check,
    recheck_rows,
    record_submitted,
)
from server.push.reminders import CHANNEL as REMINDER_CHANNEL
from server.push.router import PushRouter
from server.push.session_reauth import announced_while_sessionless, send_after
from server.push.sync_trigger import MIN_GAP as SYNC_TRIGGER_GAP
from server.syncjobs.moodle_client import SubmissionStatusChecker

logger = structlog.get_logger(__name__)

# "TD_PUSH" — serializes the claim phase across workers.
_CLAIM_LOCK_KEY = 0x54445F50555348

_UNREGISTERED_APNS_DESCRIPTIONS = {"baddevicetoken", "unregistered"}

# The schedule scenarios that announce a class. A school holiday or a day
# outside the term silences these and not `assignmentUrgent`: a deadline on
# a day off is still a deadline — the same split the app's own scenario
# resolver makes.
_CLASS_SCENARIOS = frozenset(
    {ScheduleScenario.class_preparing.value, ScheduleScenario.in_class.value}
)

# Channels whose every job is filed for one device: Live Activity starts and
# ends, and the portal's custom pushes.
_DEVICE_CHANNELS = frozenset({SCHEDULE_CHANNEL, "custom"})

# A reminder due right after its countdown start went out, to a device whose
# app has not registered the activity yet, waits once: the app registers within
# seconds, and the reminder follows the start in the same tick. Only a reminder
# this close to the start's send waits, and only once: a wait pushes the job's
# `available_at` a minute past its fire time. Each round before the first
# delivery asks Moodle again.
_COUNTDOWN_WAIT_UNTIL = timedelta(seconds=30)
_COUNTDOWN_WAIT = timedelta(seconds=60)

# Pre-send checks of a batch out to Moodle at once.
_PRESEND_CONCURRENCY = 4


@dataclass(frozen=True)
class PushPipelineWorker:
    session_factory: async_sessionmaker[AsyncSession]
    settings: Settings
    router: PushRouter
    worker_id: str
    # The pre-send check's way to Moodle. Without either, no assignment
    # reminder or countdown start can be confirmed, so none is sent.
    cipher: CredentialCipher | None = None
    fetcher: SubmissionStatusChecker | None = None


def _is_unregistered(status: str | None, description: str | None) -> bool:
    s = (status or "").lower()
    d = (description or "").lower()
    return (
        s == "410"
        or d in _UNREGISTERED_APNS_DESCRIPTIONS
        or is_dead_fcm_token(status, description)
    )


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
    """One scheduler tick: recover stale jobs, then claim and process due
    jobs a batch at a time until none are left. Returns the number of jobs
    processed.

    Draining, rather than one batch per tick: the end of a class period
    puts every phone's Live Activity end, next-class start and reminders
    due in the same minute, and one batch per tick left the last of them
    minutes late. The batch size only bounds how many jobs sit claimed at
    once. A job this tick already handled is not claimed again. Every path
    that returns one to pending pushes its `available_at` out, but a drain
    that outlasts that delay finds it due once more; it is left pending for
    the next tick. Claimed and passed over instead, it would sit in
    `processing` until stale recovery, minutes late and an attempt down.

    A batch is sent one job at a time, so a Live Activity end that comes
    due mid-batch would wait for every job claimed ahead of it: ten sends
    to a hung provider, each up to its timeout. Before each job after the
    first, the tick checks for a due job that outranks it, and if one is
    waiting hands the rest of the batch back and claims again. The first
    job of every batch always goes, so the drain always moves.
    """
    await _recover_stale_jobs(worker)
    processed: set[int] = set()
    breaker = MoodleBreaker()
    while True:
        claimed = await _claim_due_jobs(worker, skip=processed)
        if not claimed:
            break
        checks = _start_checks(worker, claimed, breaker)
        try:
            for index, (job_id, priority, _) in enumerate(claimed):
                if index and await _outranked(worker, priority, skip=processed):
                    await _release_claimed(
                        worker, [jid for jid, _, _ in claimed[index:]]
                    )
                    break
                await _process_job(
                    worker, job_id=job_id, pending_check=checks.pop(job_id, None)
                )
                processed.add(job_id)
        finally:
            for task in checks.values():
                task.cancel()
            await asyncio.gather(*checks.values(), return_exceptions=True)
    return len(processed)


def _start_checks(
    worker: PushPipelineWorker,
    claimed: list[tuple[int, int, bool]],
    breaker: MoodleBreaker,
) -> dict[int, asyncio.Task[PresendCheck | None]]:
    """Start the pre-send checks of a claimed batch, a few at a time, ahead
    of its sends: one by one, a deadline rush's Moodle calls held every job
    behind them (#98)."""
    limit = asyncio.Semaphore(_PRESEND_CONCURRENCY)
    alone = asyncio.Lock()

    async def check(job_id: int) -> PresendCheck | None:
        # In claim order until Moodle answers in this tick, so an outage still
        # costs one call (`MoodleBreaker`).
        if not breaker.answered:
            async with alone:
                if not breaker.answered:
                    return await check_before_send(
                        worker, job_id=job_id, breaker=breaker
                    )
        async with limit:
            return await check_before_send(worker, job_id=job_id, breaker=breaker)

    return {
        job_id: asyncio.create_task(check(job_id))
        for job_id, _, checked in claimed
        if checked
    }


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


def _due(now: datetime, skip: Collection[int]) -> tuple[ColumnElement[bool], ...]:
    """The pending jobs a claim may take: due, and not in `skip`.

    `skip` goes to Postgres as one array parameter rather than an IN list:
    a tick that drains a large send can have handled more jobs than a
    statement may carry parameters.
    """
    return (
        PushJob.status == PushJobStatus.pending.value,
        PushJob.fire_at <= now,
        PushJob.available_at <= now,
        PushJob.id != all_(literal(sorted(skip), ARRAY(BigInteger))),
    )


async def _claim_due_jobs(
    worker: PushPipelineWorker, *, skip: Collection[int] = ()
) -> list[tuple[int, int, bool]]:
    """Claim up to a batch of due jobs, leaving out the ids in `skip`.
    Returns (id, priority, whether the pre-send check guards it), most
    urgent first."""
    now = datetime.now(UTC)
    async with session_scope(worker.session_factory) as session:
        await session.execute(select(func.pg_advisory_xact_lock(_CLAIM_LOCK_KEY)))
        jobs = (
            (
                await session.execute(
                    select(PushJob)
                    .where(*_due(now, skip))
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
        return [(job.id, job.priority, needs_check(job)) for job in jobs]


async def _outranked(
    worker: PushPipelineWorker, priority: int, *, skip: Collection[int]
) -> bool:
    """Whether a job a claim would take now outranks `priority`.

    `skip` is the tick's handled jobs, as in the claim. One of them due
    again is left for the next tick, so it must not count here either, or
    every later job in the drain would yield to a job no claim will take.
    """
    async with session_scope(worker.session_factory) as session:
        return bool(
            await session.scalar(
                select(
                    select(PushJob.id)
                    .where(
                        *_due(datetime.now(UTC), skip),
                        PushJob.priority < priority,
                    )
                    .exists()
                )
            )
        )


async def _release_claimed(worker: PushPipelineWorker, job_ids: list[int]) -> None:
    """Hand claimed jobs back to pending, untouched, for the next claim.

    No attempt is spent and `available_at` is left alone: nothing was sent.
    """
    async with session_scope(worker.session_factory) as session:
        await session.execute(
            update(PushJob)
            .where(
                PushJob.id.in_(job_ids),
                PushJob.status == PushJobStatus.processing.value,
                PushJob.locked_by == worker.worker_id,
            )
            .values(
                status=PushJobStatus.pending.value, locked_by=None, locked_at=None
            )
        )


async def _process_job(
    worker: PushPipelineWorker,
    *,
    job_id: int,
    pending_check: Awaitable[PresendCheck | None] | None,
) -> None:
    try:
        # Asked before the job's row lock is taken: the check may wait on
        # Moodle, and no transaction stays open while it does.
        check = await pending_check if pending_check is not None else None
        async with session_scope(worker.session_factory) as session:
            await lift_timeouts(session)
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
            if check is not None:
                if await _held_back(worker, session, job, check):
                    return
            elif needs_check(job) and not await _has_deliveries(session, job):
                # The check saw this job unclaimed: another tick, under the
                # same worker id, recovered and claimed it again meanwhile.
                # Unchecked, it goes back for the next round to check.
                job.status = PushJobStatus.pending.value
                job.locked_by = None
                job.locked_at = None
                logger.warning("push.presend_check_missed", job_id=job.id)
                return
            if await _materialize(
                session,
                job,
                school_tz=worker.settings.course_reminder_timezone,
                period_times=worker.settings.course_period_start_times,
                countdown_window=timedelta(
                    seconds=worker.settings.push_countdown_dedupe_window_seconds
                ),
                presend_grace=timedelta(
                    minutes=worker.settings.push_presend_grace_minutes
                ),
            ):
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


async def _materialize(
    session: AsyncSession,
    job: PushJob,
    *,
    school_tz: str,
    period_times: dict[str, str],
    countdown_window: timedelta,
    presend_grace: timedelta,
) -> bool:
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

    Returns False when the job was settled or put back here and must not be
    delivered now.

    Skips macOS devices (foreground-only, no background push). Non-macOS
    devices always receive the push — collapse keys deduplicate on the
    device side, and foreground devices simply ignore redundant syncs.
    """

    now = datetime.now(UTC)
    payload = job.payload or {}
    if job.device_id is None and job.channel in _DEVICE_CHANNELS:
        # Its device row was deleted since (ON DELETE SET NULL, as when another
        # account signs in on that phone), and the token query below would then
        # reach every device of the user. Rounds already begun only finish.
        if await _has_deliveries(session, job):
            return True
        _settle(job, status=PushJobStatus.cancelled, error="device_gone", now=now)
        logger.info("push.device_gone", job_id=job.id, channel=job.channel)
        return False
    is_activity_job = job.channel == SCHEDULE_CHANNEL
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

    is_session_reauth = (
        job.scenario == REAUTH_SCENARIO
        and payload.get("reason") == SESSION_REAUTH_REASON
    )
    announced_at = (
        await announced_while_sessionless(session, job.device_id, now)
        if is_session_reauth and job.device_id is not None
        else None
    )
    if is_session_reauth and announced_at is None:
        # Signed in again, signed out, removed or TigerSync off since the scan
        # filed it, asked on every round as a retry can come minutes after a
        # first try that failed. A job's rounds go on only while its delivery
        # is pending, to its one device's one token, so nothing sent is
        # reported as cancelled; a deleted device row would leave the token
        # query above reaching the user's other devices.
        await session.execute(
            update(PushDelivery)
            .where(
                PushDelivery.push_job_id == job.id,
                PushDelivery.status == PushDeliveryStatus.pending.value,
            )
            .values(
                status=PushDeliveryStatus.skipped.value,
                failure_code="stale_before_send",
            )
        )
        _settle(job, status=PushJobStatus.cancelled, error="stale_before_send", now=now)
        logger.info("push.session_reauth_stale", job_id=job.id)
        return False
    if is_session_reauth and (
        wait_until := send_after(now, announced_at, ZoneInfo(school_tz))
    ):
        # At night at school, as for a retry past 21:00 or a job left pending
        # over a restart, or right after the app announced itself, before its
        # own refresh or sign-in could land. Back to pending without spending
        # an attempt, as for an unverified pre-send check (`_held_back`), and
        # asked again then.
        job.status = PushJobStatus.pending.value
        job.available_at = wait_until
        job.locked_by = None
        job.locked_at = None
        logger.info(
            "push.session_reauth_waits", job_id=job.id, until=wait_until.isoformat()
        )
        return False

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
        elif (
            job.scenario in _CLASS_SCENARIOS
            and (
                quiet := await _classes_quiet(
                    session, user_id=job.user_id, day=_class_day(job, school_tz)
                )
            )
            and not await _has_deliveries(session, job)
        ):
            # The class this would announce does not meet. The app files a
            # class's starts up to a day ahead, older builds without asking
            # the school calendar, so the day is decided again here, when the
            # start fires — which also lets a term or holiday published since,
            # or a "還要上課？" toggle flipped since, reach a job already
            # waiting. Cancelled rather than failed, and only before the first
            # delivery, for the reasons given for a running activity below.
            _settle(job, status=PushJobStatus.cancelled, error=quiet, now=now)
            logger.info(
                "push.activity_quiet_day",
                job_id=job.id,
                activity_id=activity_id,
                reason=quiet,
            )
            return False
        elif (
            job.scenario in _CLASS_SCENARIOS
            and not await _has_deliveries(session, job)
            and await _held_by_course_changes(
                session, job, school_tz=school_tz, period_times=period_times
            )
        ):
            return False
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

    if (
        job.channel == COURSE_CHANNEL
        and not await _has_deliveries(session, job)
        and await _held_by_course_changes(
            session, job, school_tz=school_tz, period_times=period_times
        )
    ):
        return False

    is_sync_trigger = job.scenario == "sync_trigger"
    source_device_id: str | None = None
    if is_sync_trigger:
        # Paced from the last send, not the planned time: downtime or a retry
        # can leave triggers due together (#107). The portal's force button is not.
        forced = (job.payload or {}).get("source") == "portal_force"
        last = (
            None if forced else await _last_sync_trigger_sent(session, job, now=now)
        )
        if last:
            job.status = PushJobStatus.pending.value
            job.available_at = last + SYNC_TRIGGER_GAP
            job.locked_by = None
            job.locked_at = None
            logger.info("push.sync_trigger_paced", job_id=job.id)
            return False
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
            UserDevice.platform.in_(APPLE_HANDHELD_PLATFORMS),
        )

    if job.channel == COURSE_CHANNEL:
        # iPhone and iPad only, listed so a client added later fails closed.
        # Android posts its own class reminders and FcmService has no
        # handler for the server's, so there a delivery is a high-priority
        # message that shows nothing — and FCM demotes an app's
        # high-priority messages once enough of them show nothing.
        token_query = token_query.where(
            UserDevice.platform.in_(APPLE_HANDHELD_PLATFORMS)
        )

    if job.channel == BULLETIN_CHANNEL:
        # The bulletin opt-out. Per-device, not per-user: matching
        # (server/bulletins/user_dispatch.py Pass A) and the push_jobs it
        # writes (Pass B) never look at this column, so an opted-out device
        # still gets its bulletin_user_matches row stamped and does not
        # receive a backlog replay the day it opts back in. Deliberately
        # independent of server_push_enabled, which this job never consults.
        token_query = token_query.where(UserDevice.bulletin_push_enabled.is_(True))
        # Subscriptions are per device, so the job names the devices whose
        # own rules matched. A job written before that names none and goes
        # to every device, as it always did.
        target_device_ids = payload.get("target_device_ids")
        if target_device_ids is not None:
            token_query = token_query.where(
                UserDevice.id.in_([uuid.UUID(d) for d in target_device_ids])
            )

    rows = (await session.execute(token_query)).all()
    if not rows:
        return True
    covered: dict[uuid.UUID, datetime] = {}
    waiting: dict[uuid.UUID, datetime] = {}
    if is_assignment_reminder:
        # Only the devices this reminder can reach: an older app's start must
        # not hold the reminder back from the others.
        covered, waiting = await _countdown_starts(
            session,
            job,
            {
                device.id
                for _, device in rows
                if not schedules_reminders_locally(device.platform, device.app_version)
            },
            window=countdown_window,
            now=now,
        )
    first_round = bool(covered or waiting) and not await _has_deliveries(
        session, job
    )
    if (
        waiting
        and first_round
        and now < max(waiting.values()) + _COUNTDOWN_WAIT_UNTIL
        and job.available_at < job.fire_at + _COUNTDOWN_WAIT
        # The round after the wait, with its tick and its Moodle check, must
        # still fall inside the pre-send grace, which would otherwise cancel
        # the reminder as stale.
        and now + 2 * _COUNTDOWN_WAIT < job.fire_at + presend_grace
    ):
        # Back to pending without spending an attempt, as for an unverified
        # pre-send check (`_held_back`).
        job.status = PushJobStatus.pending.value
        job.available_at = now + _COUNTDOWN_WAIT
        job.locked_by = None
        job.locked_at = None
        logger.info(
            "push.reminder_waits_for_countdown",
            job_id=job.id,
            devices=_sent_times(waiting),
        )
        metrics.observe_countdown_dedupe("held")
        return False
    left_out: dict[uuid.UUID, datetime] = {}
    values = []
    for token, device in rows:
        if device.platform == UserDevicePlatform.macos.value:
            continue
        if is_assignment_reminder and schedules_reminders_locally(
            device.platform, device.app_version
        ):
            # A client that still has its own scheduler would show this
            # reminder twice. Version comparison cannot go in the query
            # above: app_version is VARCHAR, and "2.10.0" sorts below
            # "2.9.0" as a string.
            continue
        if device.id in covered:
            # The countdown start's alert rang here for this moment, and the
            # app cannot drop a remote notification.
            left_out[device.id] = covered[device.id]
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
                # Due now by the clock `_deliver_round` reads: this host's.
                # Left to its server default, the database clock's now(), a
                # database clock even a few milliseconds ahead makes every
                # delivery this tick creates wait out a retry round.
                "next_retry_at": now,
            }
        )
    if first_round and left_out:
        logger.info(
            "push.reminder_left_out_for_countdown",
            job_id=job.id,
            devices=_sent_times(left_out),
        )
        metrics.observe_countdown_dedupe("left_out", len(left_out))
    if first_round and waiting:
        # Waited once already, or too late to wait: two alerts beat none.
        logger.info(
            "push.countdown_not_registered",
            job_id=job.id,
            devices=_sent_times(waiting),
        )
        metrics.observe_countdown_dedupe("unregistered", len(waiting))
    if not values:
        if left_out and first_round:
            # Every device that would take it already rang for this moment.
            # Only before the first delivery, as for a running activity above.
            _settle(
                job,
                status=PushJobStatus.cancelled,
                error="countdown_start_sent",
                now=now,
            )
            return False
        return True
    await session.execute(
        pg_insert(PushDelivery)
        .values(values)
        .on_conflict_do_nothing(
            index_elements=["push_job_id", "token_hash", "token_kind", "scope_key"]
        )
    )
    return True


# A held class start is checked again at least this often, so an edit that
# shortens or removes its change wakes it in time.
_HELD_START_RECHECK = timedelta(minutes=5)


async def _held_by_course_changes(
    session: AsyncSession,
    job: PushJob,
    *,
    school_tz: str,
    period_times: dict[str, str],
) -> bool:
    """Applies the school's course changes to a class reminder or class Live
    Activity start right before its first delivery; True when that settled
    the job or put it back.

    Fails closed: a push whose class the check cannot confirm is cancelled
    (`unverified_before_send`), and so is a reminder whose class was
    cancelled or moved and a start whose class was cancelled
    (`class_changed`). A start whose class begins later waits as long.
    """
    now = datetime.now(UTC)
    delay = timedelta(0)
    try:
        # A savepoint, so a failed query leaves the job's transaction usable.
        async with session.begin_nested():
            tz = ZoneInfo(school_tz)
            if job.channel == COURSE_CHANNEL:
                payload = await recheck_class_reminder(
                    session, job.payload or {}, tz=tz, period_times=period_times
                )
            else:
                payload, delay = await recheck_class_start(
                    session, job.payload or {}, tz=tz
                )
    except Exception:
        logger.exception("push.course_changes_check_failed", job_id=job.id)
        _settle(
            job, status=PushJobStatus.cancelled, error="unverified_before_send", now=now
        )
        return True
    if payload is None:
        _settle(job, status=PushJobStatus.cancelled, error="class_changed", now=now)
        logger.info("push.class_changed", job_id=job.id, channel=job.channel)
        return True
    if job.fire_at + delay > now:
        # Only `available_at` moves and the filed payload stays, so the app's
        # re-sync keeps the job and a later check measures from the filed start.
        job.status = PushJobStatus.pending.value
        job.available_at = min(job.fire_at + delay, now + _HELD_START_RECHECK)
        job.locked_by = None
        job.locked_at = None
        logger.info(
            "push.class_start_moved", job_id=job.id, until=job.available_at.isoformat()
        )
        return True
    job.payload = payload
    return False


def _settle(job: PushJob, *, status: PushJobStatus, error: str, now: datetime) -> None:
    """Finish `job` here, without a delivery round."""
    job.status = status.value
    job.last_error = error
    if status is PushJobStatus.cancelled:
        job.cancelled_at = now
    job.locked_by = None
    job.locked_at = None


async def _held_back(
    worker: PushPipelineWorker, session: AsyncSession, job: PushJob, check: PresendCheck
) -> bool:
    """Act on the pre-send check's verdict in the transaction that holds the
    job; True when the job is not to be delivered now.

    A `send` verdict stands only if the rows, read again here under the
    job's lock, still say the job is due (they were read before the Moodle
    call), and only within the grace after the job's fire time. An
    `unverified` job goes back to pending without spending an attempt, since
    nothing was sent, until that grace has passed; then it is cancelled.
    """
    now = datetime.now(UTC)
    settings = worker.settings
    grace_end = job.fire_at + timedelta(minutes=settings.push_presend_grace_minutes)
    deferred = False
    if check.verdict == "send":
        check = await recheck_rows(session, job, now) or check
    if check.verdict == "send" and now >= grace_end:
        # The copy counts from the fire time ("30 minutes left"), which is
        # wrong this late, whether the job was claimed late or retried.
        check = PresendCheck("gone", "past_grace", check.moodle_assignment_id)
    if check.verdict == "submitted":
        _settle(
            job, status=PushJobStatus.cancelled, error="submitted_before_send", now=now
        )
        await record_submitted(session, user_id=job.user_id, check=check, now=now)
    elif check.verdict == "gone":
        _settle(job, status=PushJobStatus.cancelled, error="stale_before_send", now=now)
    elif check.verdict == "unverified":
        if now < grace_end:
            job.status = PushJobStatus.pending.value
            job.available_at = now + timedelta(
                seconds=settings.push_presend_retry_seconds
            )
            job.locked_by = None
            job.locked_at = None
            deferred = True
        else:
            _settle(
                job,
                status=PushJobStatus.cancelled,
                error="unverified_before_send",
                now=now,
            )
    logger.info(
        "push.presend_check",
        job_id=job.id,
        channel=job.channel,
        verdict=check.verdict,
        reason=check.reason,
        deferred=deferred,
    )
    metrics.observe_presend_check(check.verdict)
    return check.verdict != "send"


def _class_day(job: PushJob, school_tz: str) -> date:
    """The day the class a start announces meets on, in the school's zone.

    Read from the snapshot's `countdownTarget` — the class start for
    `classPreparing`, its end for `inClass`, both on the day the class
    meets — because `/schedule/sync` does not tie `fire_at` to that day: a
    start filed to fire the evening before would be checked against the
    wrong date. A snapshot with no usable target falls back to the day the
    start fires. The zone is resolved here, for class starts alone, so a
    bad setting cannot stop any other push.
    """
    target = _to_unix_seconds((job.payload or {}).get("countdownTarget"))
    try:
        moment = (
            datetime.fromtimestamp(target, UTC) if target is not None else job.fire_at
        )
    except (OverflowError, OSError, ValueError):
        # A target no calendar can hold is malformed client data; the day
        # the start fires is the best remaining guess.
        moment = job.fire_at
    return moment.astimezone(ZoneInfo(school_tz)).date()


async def _classes_quiet(
    session: AsyncSession, *, user_id: uuid.UUID, day: date
) -> Literal["outside_term", "holiday"] | None:
    """Why classes do not meet for `user_id` on `day`, or None when they do.

    The rule `course_reminders.scan_course_reminders` applies to the class
    reminder alerts, and the apps' `isClassDay`.

    * `outside_term`: no published term runs on the day.
    * `holiday`: a holiday covers the day and the user has not opted in to
      any holiday that does. Opting in to one un-silences the day even when
      a second, overlapping holiday covers it too, but not a day outside
      the term.
    """
    if outside_term(day, await load_term_dates(session)):
        return "outside_term"
    covering = (
        (
            await session.execute(
                select(AcademicHoliday.id).where(
                    AcademicHoliday.start_date <= day,
                    AcademicHoliday.end_date >= day,
                )
            )
        )
        .scalars()
        .all()
    )
    if not covering:
        return None
    opted_in = (
        await session.execute(
            select(UserHolidayOverride.id)
            .where(
                UserHolidayOverride.user_id == user_id,
                UserHolidayOverride.holiday_id.in_(covering),
                UserHolidayOverride.notify.is_(True),
            )
            .limit(1)
        )
    ).first()
    return "holiday" if opted_in is None else None


async def _last_sync_trigger_sent(
    session: AsyncSession, job: PushJob, *, now: datetime
) -> datetime | None:
    """When another of the user's sync triggers reached a device within the
    gap, or None."""
    return await session.scalar(
        select(func.max(PushDelivery.sent_at)).where(
            PushDelivery.push_job_id.in_(
                select(PushJob.id).where(
                    PushJob.user_id == job.user_id,
                    PushJob.scenario == "sync_trigger",
                    PushJob.id != job.id,
                    # A job with a sent delivery has one of these statuses;
                    # as SQL text the user's jobs come from its index.
                    PUSH_JOB_DEDUPE_ACTIVE_WHERE,
                )
            ),
            PushDelivery.status == PushDeliveryStatus.sent.value,
            PushDelivery.sent_at > now - SYNC_TRIGGER_GAP,
        )
    )


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
    `activity_id` and its countdown has not passed (`_registered`).

    Every schedule job carries a device: `/schedule/sync` refuses a
    session without one, as `/live-activities/register` does.
    """
    return bool(
        await _registered(
            session,
            user_id=job.user_id,
            device_ids=[job.device_id],
            activity_id=activity_id,
            now=now,
        )
    )


async def _registered(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    device_ids: Collection[uuid.UUID | None],
    activity_id: str,
    now: datetime,
) -> set[uuid.UUID | None]:
    """The devices among `device_ids` that have registered `activity_id`
    and whose countdown has not passed.

    `/live-activities/register` files the activity's end job under the
    device's `la_end` key at its countdown target; while that job is still
    waiting to fire, the activity is on screen. Only a pending job can be
    waiting: the claim only takes jobs whose `fire_at` has passed, and
    re-registering with a later target puts the job back to pending. The
    token rows cannot say this — an update token stays active after its
    activity ends until something is pushed to it and APNs answers 410.
    """
    keys = {
        activity_end_key(device_id, activity_id): device_id for device_id in device_ids
    }
    rows = await session.execute(
        select(PushJob.dedupe_key).where(
            PushJob.user_id == user_id,
            PushJob.dedupe_key.in_(list(keys)),
            # SQL text, so a generic plan still reads ux_push_jobs_dedupe_active.
            text("status = 'pending'"),
            PushJob.fire_at > now,
        )
    )
    return {keys[key] for key in rows.scalars()}


async def _countdown_starts(
    session: AsyncSession,
    job: PushJob,
    device_ids: set[uuid.UUID],
    *,
    window: timedelta,
    now: datetime,
) -> tuple[dict[uuid.UUID, datetime], dict[uuid.UUID, datetime]]:
    """When the countdown start of reminder `job`'s assignment went out to
    each of `device_ids` it reached at most `window` before the reminder's
    fire time, or since, as `(covered, waiting)`: whether the app there has
    registered the activity the start created, or not yet (a start still
    being delivered counts as sent at `now`).

    The start is the device's own `/schedule/sync` job for the assignment,
    the one the pre-send check guards, and counts once a delivery of it was
    sent on a job still sent or retrying. A start that failed, was
    cancelled, found no push-to-start token or was never filed leaves the
    device its reminder. A sent one is not yet a countdown on screen: iOS
    shows nothing for a start it cannot show, and the app ends one it does
    not want without registering it. Read from the fire time rather than
    the clock, so every round of the job finds the same starts.
    """
    assignment_id = _assignment_id(job)
    if not window or assignment_id is None or not device_ids:
        return {}, {}
    source_id = str(assignment_id)
    scenario = ScheduleScenario.assignment_urgent.value
    keys = {
        schedule_key(device_id, source_id, scenario): device_id
        for device_id in device_ids
    }
    starts = select(PushJob.id).where(
        PushJob.user_id == job.user_id,
        PushJob.dedupe_key.in_(list(keys)),
        # SQL text, so a generic plan still reads ux_push_jobs_dedupe_active:
        # a start that went out is sent, partial_failed or still retrying.
        PUSH_JOB_DEDUPE_ACTIVE_WHERE,
    )
    rows = await session.execute(
        select(PushDelivery.device_id, PushDelivery.sent_at).where(
            PushDelivery.push_job_id.in_(starts),
            PushDelivery.status == PushDeliveryStatus.sent.value,
            PushDelivery.sent_at >= job.fire_at - window,
        )
    )
    sent = {device_id: at for device_id, at in rows}
    # A start another tick is still delivering has no sent delivery until that
    # tick commits; the reminder waits for it as for one sent now (#114).
    claimed = await session.scalars(
        select(PushJob.dedupe_key).where(
            PushJob.user_id == job.user_id,
            PushJob.dedupe_key.in_(list(keys)),
            text("status = 'processing'"),
            PushJob.fire_at >= job.fire_at - window,
        )
    )
    in_flight = {keys[key]: now for key in claimed if keys[key] not in sent}
    if not sent and not in_flight:
        return {}, {}
    registered = await _registered(
        session,
        user_id=job.user_id,
        device_ids=sent.keys(),
        activity_id=_activity_id(source_id, scenario),
        now=now,
    )
    covered = {d: at for d, at in sent.items() if d in registered}
    waiting = {d: at for d, at in sent.items() if d not in registered} | in_flight
    return covered, waiting


def _sent_times(starts: dict[uuid.UUID, datetime]) -> dict[str, str]:
    """For the log: each device id with when its countdown start went out."""
    return {str(device_id): at.isoformat() for device_id, at in starts.items()}


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
    is_reauth = job.scenario == REAUTH_SCENARIO
    if is_reauth or job.channel in (REMINDER_CHANNEL, COURSE_CHANNEL):
        # Copy is resolved here, not at enqueue time: one job fans out to
        # every device on the account and they can be in different
        # languages, so the only place the right language is known is the
        # recipient. `session.get` is an identity-map hit — `_materialize`
        # loaded this device earlier in the same transaction.
        #
        # Reauth is keyed on the job's scenario, not payload["kind"]: the
        # portal's operator "retry + notify" endpoints (portal/app/routes/
        # moodle/jobs.py) insert reauth_required jobs directly via SQL in
        # the pre-v2.1.0 payload shape (no `kind`), and always will — the
        # portal is a separate package that talks raw SQL and does not
        # import this module. Keying on `kind` let those jobs skip copy
        # rebuild entirely and ship the empty-banner bug this branch exists
        # to fix.
        device = await session.get(UserDevice, delivery.device_id)
        locale = device.locale if device is not None else None
        if is_reauth:
            payload = build_reauth_payload(
                provider=payload.get("provider", ""),
                reason=payload.get("reason"),
                locale=locale,
            )
        elif job.channel == COURSE_CHANNEL:
            payload = build_course_reminder_payload(payload, locale=locale)
        else:
            payload = build_assignment_reminder_payload(payload, locale=locale)
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
        request = build_apns_for_job(
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
        send = worker.router.send_apple
    else:
        request = build_fcm_for_job(
            payload=payload, channel=job.channel, token_value=token.token_value
        )
        send = worker.router.send_android
    try:
        result = await send(request)
    except Exception as exc:
        # aioapns raises on connection trouble. Raised past here, it would roll
        # back the round, and the deliveries it already sent would go out again.
        logger.warning(
            "push.send_raised",
            job_id=job.id,
            delivery_id=delivery.id,
            error=type(exc).__name__,
        )
        result = SendResult(success=False, status="exception", description=str(exc))

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
