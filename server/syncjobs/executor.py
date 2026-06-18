"""Server-side sync job executor (sync-and-push spec §4).

Claim/execute split: one short transaction recovers stale locks, another
claims up to `sync_job_batch_size` due jobs (`FOR UPDATE SKIP LOCKED`,
joined to an enabled+in-window policy, global running-count cap), then
each job executes in its own transaction so one failure can't poison the
batch. Jobs run sequentially within a tick — deliberate politeness toward
the school APIs behind our single egress IP.

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
    MoodleRateLimited,
    MoodleTokenInvalid,
    MoodleUnreachable,
)

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


@dataclass(frozen=True)
class SyncWorker:
    session_factory: async_sessionmaker[AsyncSession]
    settings: Settings
    cipher: CredentialCipher
    fetcher: AssignmentFetcher
    course_fetcher: CourseFetcher
    worker_id: str


async def run_sync_tick(worker: SyncWorker) -> int:
    """One scheduler tick: recover stale locks, claim due jobs, execute
    them sequentially. Returns the number of jobs executed."""
    await _recover_stale_jobs(worker)
    claimed = await _claim_due_jobs(worker)
    for job_id, run_id in claimed:
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

            now = datetime.now(UTC)
            if job.job_type == SyncJobType.ntust_courses.value:
                try:
                    courses = await worker.course_fetcher.fetch_courses(token=token)
                except MoodleTokenInvalid:
                    raise CredentialInvalid("moodle_token_invalid")
                stats = await apply_fetched_courses(
                    session, user_id=job.user_id, fetched=courses, now=now
                )
            else:
                try:
                    fetched = await worker.fetcher.fetch_assignments(token=token)
                except MoodleTokenInvalid:
                    raise CredentialInvalid("moodle_token_invalid")
                stats = await apply_fetched_assignments(
                    session, user_id=job.user_id, fetched=fetched, now=now
                )

            if run is not None:
                run.status = SyncRunStatus.succeeded.value
                run.finished_at = now
                run.fetched_count = stats.fetched_count
                run.changed_count = stats.changed_count

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
                job_id=job_id,
                user_id=str(job.user_id),
                fetched=stats.fetched_count,
                changed=stats.changed_count,
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
