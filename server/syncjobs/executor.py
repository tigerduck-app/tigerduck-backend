"""Server-side sync job executor (sync-and-push spec §4).

Claim/execute split: one short transaction recovers stale locks, another
claims up to `sync_job_batch_size` due jobs (`FOR UPDATE SKIP LOCKED`,
joined to an enabled+in-window policy, global running-count cap), then
each job executes in its own transaction so one failure can't poison the
batch; an assignments job then probes submission status in transactions
of its own (`_refresh_submission_status`). Jobs run sequentially within a
tick — deliberate politeness toward the school APIs behind our single
egress IP.

Failure taxonomy → outcome:
* `CredentialInvalid`            → account invalidated, ALL user jobs
                                   disabled, reauth push queued. No retry.
* `MoodleRateLimited`            → retriable, backoff, last_error
                                   'school_rate_limited'.
* `MoodleUnreachable`/`SsoUnavailable`/unexpected
                                 → retriable, backoff, last_error
                                   'sync_failed:<detail>'.
Retriable failures exceeding max_attempts land in status 'failed' until
pull-to-refresh or re-login revives them.
"""

from __future__ import annotations

import asyncio
import os
import socket
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.auth.crypto import CredentialCipher
from server.auth.models import ExternalAccount
from server.config import Settings
from server.db import session_scope
from server.syncjobs.assignments import apply_fetched_assignments
from server.syncjobs.availability import is_moodle_available
from server.syncjobs.courses import apply_fetched_courses
from server.syncjobs.credentials import (
    CredentialInvalid,
    load_credential_blob,
    mark_credentials_invalid,
)
from server.syncjobs.models import (
    SyncJob,
    SyncJobStatus,
    SyncJobType,
    SyncPolicy,
    SyncRun,
    SyncRunStatus,
)
from server.syncjobs.moodle_client import (
    AssignmentFetcher,
    CourseFetcher,
    MoodleClientError,
    MoodleRateLimited,
    MoodleTokenInvalid,
    MoodleUnreachable,
    _without_token,
)
from server.syncjobs.submissions import apply_submission_status, select_assignment_ids
from server.push.submission_cancel import cancel_for_submitted

from server.auth.models import PushJob
from sqlalchemy.dialects.postgresql import insert as pg_insert

logger = structlog.get_logger(__name__)

ERROR_CREDENTIAL_INVALID = "credential_invalid"
ERROR_SCHOOL_RATE_LIMITED = "school_rate_limited"
ERROR_SYNC_FAILED = "sync_failed"

_DEFAULT_INTERVAL_SECONDS = 28800
_DEFAULT_PRIORITY = 100
# pg advisory lock key ("TD_SYNC") serializing the claim phase across all
# workers — the running-count check and the claim must be atomic, or N
# workers could each claim a full batch and collectively blow the global
# concurrency cap.
_CLAIM_LOCK_KEY = 0x54445F53594E43


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


async def _enqueue_sync_trigger(session: AsyncSession, user_id) -> None:
    now = datetime.now(UTC)
    stmt = (
        pg_insert(PushJob)
        .values(
            user_id=user_id,
            dedupe_key=f"sync_trigger:{user_id}:{int(now.timestamp()) // 300}",
            channel="system",
            scenario="sync_trigger",
            fire_at=now,
            payload={"kind": "sync_trigger", "source_device_id": None},
        )
        .on_conflict_do_nothing()
    )
    await session.execute(stmt)


@dataclass(frozen=True)
class SyncWorker:
    session_factory: async_sessionmaker[AsyncSession]
    settings: Settings
    cipher: CredentialCipher
    fetcher: AssignmentFetcher
    course_fetcher: CourseFetcher
    worker_id: str


def _in_maintenance_window(window: str) -> bool:
    """Return True if current UTC time falls within the maintenance window.

    *window* format: ``"HH:MM-HH:MM"`` (start-end, UTC).  Wrapping past
    midnight is supported (e.g. ``"23:00-02:00"``).  Empty string or
    unparseable values → ``False`` (no window).
    """
    if not window or "-" not in window:
        return False
    try:
        start_str, end_str = window.split("-", 1)
        sh, sm = (int(x) for x in start_str.strip().split(":"))
        eh, em = (int(x) for x in end_str.strip().split(":"))
    except (ValueError, TypeError):
        return False
    now = datetime.now(UTC)
    now_minutes = now.hour * 60 + now.minute
    start_minutes = sh * 60 + sm
    end_minutes = eh * 60 + em
    if start_minutes <= end_minutes:
        return start_minutes <= now_minutes < end_minutes
    # Window wraps past midnight (e.g. 23:00-02:00).
    return now_minutes >= start_minutes or now_minutes < end_minutes


async def run_sync_tick(worker: SyncWorker) -> int:
    """One scheduler tick: recover stale locks, claim due jobs, execute
    them sequentially. Returns the number of jobs executed."""
    if _in_maintenance_window(worker.settings.sync_maintenance_window):
        logger.info(
            "syncjobs.maintenance_window",
            window=worker.settings.sync_maintenance_window,
        )
        return 0
    await _recover_stale_jobs(worker)
    claimed = await _claim_due_jobs(worker)
    for idx, (job_id, run_id) in enumerate(claimed):
        if idx > 0:
            await asyncio.sleep(worker.settings.sync_job_min_interval_seconds)
        await _execute_job(worker, job_id=job_id, run_id=run_id)
    return len(claimed)


async def _recover_stale_jobs(worker: SyncWorker) -> None:
    cutoff = datetime.now(UTC) - timedelta(
        minutes=worker.settings.sync_job_stale_lock_minutes
    )
    async with session_scope(worker.session_factory) as session:
        jobs = (
            (
                await session.execute(
                    select(SyncJob)
                    .where(
                        SyncJob.status == SyncJobStatus.running.value,
                        SyncJob.locked_at < cutoff,
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
            # A stale lock means a worker died (or hung) mid-run — count it
            # as an attempt so a crash-looping job still terminates at
            # max_attempts instead of retrying forever.
            job.attempts += 1
            job.last_failure_at = now
            job.last_error = f"{ERROR_SYNC_FAILED}:stale_lock"
            if job.attempts >= job.max_attempts:
                job.status = SyncJobStatus.failed.value
            else:
                job.status = SyncJobStatus.pending.value
        await session.execute(
            update(SyncRun)
            .where(
                SyncRun.sync_job_id.in_([j.id for j in jobs]),
                SyncRun.status == SyncRunStatus.running.value,
            )
            .values(
                status=SyncRunStatus.failed.value,
                error="stale_lock_recovered",
                finished_at=now,
            )
        )
        logger.warning(
            "syncjobs.stale_recovered", count=len(jobs), worker=worker.worker_id
        )


async def _claim_due_jobs(worker: SyncWorker) -> list[tuple[int, int]]:
    now = datetime.now(UTC)
    async with session_scope(worker.session_factory) as session:
        # Serialize the count+claim across workers (see _CLAIM_LOCK_KEY).
        # Released automatically at transaction end.
        await session.execute(select(func.pg_advisory_xact_lock(_CLAIM_LOCK_KEY)))
        running = (
            await session.execute(
                select(func.count())
                .select_from(SyncJob)
                .where(SyncJob.status == SyncJobStatus.running.value)
            )
        ).scalar_one()
        capacity = min(
            worker.settings.sync_job_batch_size,
            worker.settings.sync_job_global_concurrency - running,
        )
        if capacity <= 0:
            return []

        jobs = (
            (
                await session.execute(
                    select(SyncJob)
                    .join(SyncPolicy, SyncPolicy.job_type == SyncJob.job_type)
                    .where(
                        SyncJob.status == SyncJobStatus.pending.value,
                        SyncJob.run_after <= now,
                        SyncPolicy.enabled.is_(True),
                        sa.or_(
                            SyncPolicy.active_from.is_(None),
                            SyncPolicy.active_from <= now,
                        ),
                        sa.or_(
                            SyncPolicy.active_until.is_(None),
                            SyncPolicy.active_until > now,
                        ),
                    )
                    .order_by(SyncJob.priority, SyncJob.run_after)
                    .limit(capacity)
                    .with_for_update(skip_locked=True, of=SyncJob)
                )
            )
            .scalars()
            .all()
        )
        claimed: list[tuple[int, int]] = []
        for job in jobs:
            job.status = SyncJobStatus.running.value
            job.locked_by = worker.worker_id
            job.locked_at = now
            run = SyncRun(sync_job_id=job.id, user_id=job.user_id)
            session.add(run)
            await session.flush()
            claimed.append((job.id, run.id))
        return claimed


async def _execute_job(worker: SyncWorker, *, job_id: int, run_id: int) -> None:
    try:
        probe_submissions = False
        async with session_scope(worker.session_factory) as session:
            job = (
                await session.execute(
                    select(SyncJob).where(SyncJob.id == job_id).with_for_update()
                )
            ).scalar_one()
            run = await session.get(SyncRun, run_id)
            if (
                job.status != SyncJobStatus.running.value
                or job.locked_by != worker.worker_id
            ):
                # Stale-recovered (and possibly reclaimed by another worker)
                # between our claim commit and now — never double-execute.
                if run is not None and run.status == SyncRunStatus.running.value:
                    run.status = SyncRunStatus.cancelled.value
                    run.finished_at = datetime.now(UTC)
                    run.error = "reclaimed_before_execution"
                logger.warning(
                    "syncjobs.job_reclaimed", job_id=job_id, worker=worker.worker_id
                )
                return
            policy = (
                await session.execute(
                    select(SyncPolicy).where(SyncPolicy.job_type == job.job_type)
                )
            ).scalar_one_or_none()

            account, blob = await load_credential_blob(
                session, worker.cipher, external_account_id=job.external_account_id
            )
            token = (blob.get("token_cache") or {}).get("moodle_token")

            if not isinstance(token, str) or not token:
                raise CredentialInvalid("moodle_token_missing")

            from .log_entries import log_sync

            await log_sync(session, user_id=job.user_id, source="executor",
                           message=f"Job started: {job.job_type}",
                           detail={"job_id": job.id, "run_id": run_id})

            now = datetime.now(UTC)
            if job.job_type == SyncJobType.ntust_courses.value:
                try:
                    courses = await worker.course_fetcher.fetch_courses(token=token)
                except MoodleTokenInvalid:
                    raise CredentialInvalid("moodle_token_invalid")
                await log_sync(session, user_id=job.user_id, source="executor",
                               message=f"Fetched {len(courses)} courses from Moodle")
                stats = await apply_fetched_courses(
                    session, user_id=job.user_id, fetched=courses, now=now
                )
            else:
                try:
                    fetched = await worker.fetcher.fetch_assignments(token=token)
                except MoodleTokenInvalid:
                    raise CredentialInvalid("moodle_token_invalid")
                await log_sync(session, user_id=job.user_id, source="executor",
                               message=f"Fetched {len(fetched)} assignments from Moodle")
                stats = await apply_fetched_assignments(
                    session, user_id=job.user_id, fetched=fetched, now=now
                )
                probe_submissions = True

            if run is not None:
                run.fetched_count = stats.fetched_count
                run.changed_count = stats.changed_count

            if stats.changed_count > 0:
                await _enqueue_sync_trigger(session, job.user_id)

            user_id = job.user_id
            if not probe_submissions:
                await _mark_succeeded(
                    session, job=job, run=run, policy=policy, stats=stats, now=now
                )

        if probe_submissions:
            # Only once the list sync above has committed; see
            # `_refresh_submission_status`. The run stays `running` until the
            # probe is done, so an escalation from it fails the run and backs
            # the job off through the handlers below exactly as a failed list
            # fetch would -- but the synced rows and their changelog entries
            # are already safe.
            try:
                await _refresh_submission_status(
                    worker, user_id=user_id, token=token, now=now
                )
            except MoodleTokenInvalid:
                raise CredentialInvalid("moodle_token_invalid")
            await _finish_after_probe(
                worker, job_id=job_id, run_id=run_id, stats=stats, now=now
            )
    except CredentialInvalid as exc:
        await _handle_moodle_failure(
            worker, job_id=job_id, run_id=run_id,
            error=ERROR_CREDENTIAL_INVALID, detail=exc.reason,
            is_token_invalid=True,
        )
    except MoodleRateLimited:
        await _record_failure(
            worker, job_id=job_id, run_id=run_id, error=ERROR_SCHOOL_RATE_LIMITED
        )
    except MoodleUnreachable as exc:
        await _handle_moodle_failure(
            worker, job_id=job_id, run_id=run_id,
            error=f"{ERROR_SYNC_FAILED}:{str(exc)[:120]}",
            is_token_invalid=False,
        )
    except Exception as exc:  # unexpected — never kill the tick loop
        logger.exception("syncjobs.run_crashed", job_id=job_id)
        await _record_failure(
            worker,
            job_id=job_id,
            run_id=run_id,
            error=f"{ERROR_SYNC_FAILED}:{type(exc).__name__}",
        )


async def _mark_succeeded(
    session: AsyncSession,
    *,
    job: SyncJob,
    run: SyncRun | None,
    policy: SyncPolicy | None,
    stats,
    now: datetime,
) -> None:
    """Close a run that succeeded and put its job back on its schedule."""
    from .log_entries import log_sync

    if run is not None:
        run.status = SyncRunStatus.succeeded.value
        run.finished_at = now

    await log_sync(session, user_id=job.user_id, source="executor",
                   message=f"Job succeeded: {job.job_type} — fetched={stats.fetched_count} changed={stats.changed_count}",
                   detail={"fetched": stats.fetched_count, "changed": stats.changed_count})

    interval = (
        policy.default_interval_seconds
        if policy
        else _DEFAULT_INTERVAL_SECONDS
    )
    job.status = SyncJobStatus.pending.value
    job.run_after = now + timedelta(seconds=interval)
    job.attempts = 0
    job.priority = policy.priority if policy else _DEFAULT_PRIORITY
    job.locked_by = None
    job.locked_at = None
    job.last_success_at = now
    job.last_error = None
    logger.info(
        "syncjobs.run_succeeded",
        job_id=job.id,
        user_id=str(job.user_id),
        fetched=stats.fetched_count,
        changed=stats.changed_count,
    )


async def _finish_after_probe(
    worker: SyncWorker, *, job_id: int, run_id: int, stats, now: datetime
) -> None:
    """Record the success of an assignments run once its probe is done.

    A fresh transaction, so the job is re-read and re-checked: its row lock
    went with the list sync's commit, and if stale-lock recovery took the
    job back while the probe ran, whoever holds it now owns its
    bookkeeping.
    """
    async with session_scope(worker.session_factory) as session:
        job = (
            await session.execute(
                select(SyncJob).where(SyncJob.id == job_id).with_for_update()
            )
        ).scalar_one_or_none()
        if (
            job is None
            or job.status != SyncJobStatus.running.value
            or job.locked_by != worker.worker_id
        ):
            logger.warning(
                "syncjobs.job_reclaimed", job_id=job_id, worker=worker.worker_id
            )
            return
        run = await session.get(SyncRun, run_id)
        policy = (
            await session.execute(
                select(SyncPolicy).where(SyncPolicy.job_type == job.job_type)
            )
        ).scalar_one_or_none()
        await _mark_succeeded(
            session, job=job, run=run, policy=policy, stats=stats, now=now
        )


async def _refresh_submission_status(
    worker: SyncWorker,
    *,
    user_id,
    token: str,
    now: datetime,
) -> None:
    """Best-effort probe of assignments about to enter a reminder window.

    Runs only after the assignment list sync has committed, in transactions
    of its own, so nothing it does can undo that sync. No transaction is
    open while it waits on Moodle -- a site-info call, then one request per
    assignment, `submission_status_max_concurrency` at a time, each allowed
    the full fetch timeout -- and in particular not the list sync's, which
    holds the user's `user_sync_state` row lock that every client writer for
    that user queues behind (`sync.changelog.lock_sync_state`).

    An assignment the probe finds newly submitted has its still-pending
    reminder cancelled and any Live Activity counting down toward it ended
    (`cancel_for_submitted`) in the same transaction as the status write:
    "marked submitted, but the reminder was never cancelled" is the broken
    state this probe exists to prevent, so the two stand or fall together.

    Failures are handled by kind:

    * `MoodleTokenInvalid` propagates for the caller to convert into
      `CredentialInvalid`, the same way every other Moodle call site in
      `_execute_job` does, so the job ends up disabled rather than
      silently retried.
    * Batch-level `MoodleRateLimited` / `MoodleUnreachable` propagate too,
      to the same backoff handling `fetch_assignments` gets: every
      remaining probe would fail identically, which is a refusal of the
      whole probe, not a single assignment's failure, so it is not this
      function's to swallow.
    * Anything else the fetch raises is unanticipated -- swallowed, logged
      at `error`, or at `warning` for a residual `MoodleClientError`,
      matching the probe's own two-tier policy for the same distinction.
    * A database error from any of the three database calls
      (`select_assignment_ids`, `apply_submission_status`,
      `cancel_for_submitted`) is not a probe failure and is never logged as
      one. Its own transaction rolls back, and it is logged under a separate
      event so it cannot be mistaken for routine Moodle noise; the next sync
      re-probes.
    """
    settings = worker.settings
    try:
        async with session_scope(worker.session_factory) as session:
            assignment_ids = await select_assignment_ids(
                session,
                user_id=user_id,
                now=now,
                window_hours=settings.submission_status_window_hours,
            )
        if not assignment_ids:
            return
        try:
            fetched = await worker.fetcher.fetch_submission_status(
                token=token,
                assignment_ids=assignment_ids,
                max_concurrency=settings.submission_status_max_concurrency,
            )
        except (MoodleTokenInvalid, MoodleRateLimited, MoodleUnreachable):
            raise
        except MoodleClientError as exc:
            logger.warning(
                "syncjobs.submissions.refresh_failed",
                error=type(exc).__name__,
            )
            return
        except Exception as exc:
            # This frame holds the token, and `exc_info` renders the message
            # of every exception in the chain, so the chain is scrubbed
            # first -- the same guard as the probe's own residual handler.
            logger.error(
                "syncjobs.submissions.refresh_failed",
                error=type(exc).__name__,
                exc_info=_without_token(exc, token),
            )
            return
        async with session_scope(worker.session_factory) as session:
            changed_ids = await apply_submission_status(
                session, user_id=user_id, fetched=fetched, now=now
            )
            if changed_ids:
                await cancel_for_submitted(
                    session,
                    user_id=user_id,
                    moodle_assignment_ids=changed_ids,
                    now=now,
                )
    except (MoodleTokenInvalid, MoodleRateLimited, MoodleUnreachable):
        raise
    except Exception:
        logger.error("syncjobs.submissions.refresh_db_error", exc_info=True)


async def _handle_moodle_failure(
    worker: SyncWorker,
    *,
    job_id: int,
    run_id: int,
    error: str,
    detail: str | None = None,
    is_token_invalid: bool,
) -> None:
    """Check availability windows before disabling a sync job.

    If Moodle is in a maintenance/suspend window, reschedule instead of
    disabling — the failure is likely transient. Only disable + push
    notification when Moodle should be available but isn't.
    """
    async with session_scope(worker.session_factory) as session:
        available, resume_at = await is_moodle_available(
            session, worker.settings
        )
    if not available and resume_at is not None:
        logger.info(
            "syncjobs.moodle_unavailable_window",
            job_id=job_id,
            resume_at=resume_at.isoformat(),
        )
        async with session_scope(worker.session_factory) as session:
            job = (
                await session.execute(
                    select(SyncJob).where(SyncJob.id == job_id).with_for_update()
                )
            ).scalar_one_or_none()
            if job is not None:
                run = await session.get(SyncRun, run_id)
                if run is not None and run.status == SyncRunStatus.running.value:
                    run.status = SyncRunStatus.failed.value
                    run.finished_at = datetime.now(UTC)
                    run.error = f"moodle_maintenance:{error}"
                job.status = SyncJobStatus.pending.value
                job.run_after = resume_at
                job.locked_by = None
                job.locked_at = None
                job.last_error = f"moodle_maintenance:{error}"
        return

    if is_token_invalid:
        await _record_failure(
            worker, job_id=job_id, run_id=run_id,
            error=error, disable=True, detail=detail,
        )
    else:
        await _record_failure(
            worker, job_id=job_id, run_id=run_id,
            error=error,
        )


async def _record_failure(
    worker: SyncWorker,
    *,
    job_id: int,
    run_id: int,
    error: str,
    disable: bool = False,
    detail: str | None = None,
) -> None:
    """Record a failure in a FRESH session — the work session may have
    rolled back (or be poisoned by a DB error)."""
    async with session_scope(worker.session_factory) as session:
        job = (
            await session.execute(
                select(SyncJob).where(SyncJob.id == job_id).with_for_update()
            )
        ).scalar_one_or_none()
        if job is None:
            return
        now = datetime.now(UTC)
        run = await session.get(SyncRun, run_id)
        if run is not None and run.status == SyncRunStatus.running.value:
            run.status = SyncRunStatus.failed.value
            run.finished_at = now
            run.error = error if detail is None else f"{error}:{detail}"

        job.locked_by = None
        job.locked_at = None
        job.last_failure_at = now
        job.last_error = error

        if disable:
            # Keep the ORM object in line with the bulk UPDATE inside
            # mark_credentials_invalid — later code in this session must
            # not observe a stale 'running'.
            job.status = SyncJobStatus.disabled.value
            account = await session.get(ExternalAccount, job.external_account_id)
            if account is not None:
                await mark_credentials_invalid(
                    session, account=account, user_id=job.user_id, error=error
                )
            logger.warning("syncjobs.run_disabled", job_id=job_id, error=error)
            return

        job.attempts += 1
        if job.attempts >= job.max_attempts:
            job.status = SyncJobStatus.failed.value
            logger.warning(
                "syncjobs.run_failed_terminal",
                job_id=job_id,
                attempts=job.attempts,
                error=error,
            )
        else:
            backoff = min(
                worker.settings.sync_job_backoff_base_seconds
                * 2 ** (job.attempts - 1),
                worker.settings.sync_job_backoff_cap_seconds,
            )
            job.status = SyncJobStatus.pending.value
            job.run_after = now + timedelta(seconds=backoff)
            logger.info(
                "syncjobs.run_retry_scheduled",
                job_id=job_id,
                attempts=job.attempts,
                backoff_seconds=backoff,
                error=error,
            )
