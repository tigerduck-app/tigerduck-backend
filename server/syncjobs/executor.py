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
from server.syncjobs.credentials import (
    CredentialInvalid,
    load_credential_blob,
    mark_credentials_invalid,
    refresh_moodle_token,
)
from server.syncjobs.models import (
    SyncJob,
    SyncJobStatus,
    SyncPolicy,
    SyncRun,
    SyncRunStatus,
)
from server.syncjobs.moodle_client import (
    AssignmentFetcher,
    MoodleRateLimited,
    MoodleTokenInvalid,
    MoodleUnreachable,
    SsoUnavailable,
    TokenObtainer,
)

logger = structlog.get_logger(__name__)

ERROR_CREDENTIAL_INVALID = "credential_invalid"
ERROR_SCHOOL_RATE_LIMITED = "school_rate_limited"
ERROR_SYNC_FAILED = "sync_failed"

_DEFAULT_INTERVAL_SECONDS = 28800
_DEFAULT_PRIORITY = 100


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


@dataclass(frozen=True)
class SyncWorker:
    session_factory: async_sessionmaker[AsyncSession]
    settings: Settings
    cipher: CredentialCipher
    fetcher: AssignmentFetcher
    token_obtainer: TokenObtainer
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
            job.status = SyncJobStatus.pending.value
            job.locked_by = None
            job.locked_at = None
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
            policy = (
                await session.execute(
                    select(SyncPolicy).where(SyncPolicy.job_type == job.job_type)
                )
            ).scalar_one_or_none()

            account, blob = await load_credential_blob(
                session, worker.cipher, external_account_id=job.external_account_id
            )
            token = (blob.get("token_cache") or {}).get("moodle_token")

            fetched = None
            if isinstance(token, str) and token:
                try:
                    fetched = await worker.fetcher.fetch_assignments(token=token)
                except MoodleTokenInvalid:
                    logger.info("syncjobs.token_expired", account_id=account.id)
            if fetched is None:
                token = await refresh_moodle_token(
                    session,
                    worker.cipher,
                    worker.token_obtainer,
                    account=account,
                    blob=blob,
                )
                fetched = await worker.fetcher.fetch_assignments(token=token)

            now = datetime.now(UTC)
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
        await _record_failure(
            worker,
            job_id=job_id,
            run_id=run_id,
            error=ERROR_CREDENTIAL_INVALID,
            disable=True,
            detail=exc.reason,
        )
    except MoodleRateLimited:
        await _record_failure(
            worker, job_id=job_id, run_id=run_id, error=ERROR_SCHOOL_RATE_LIMITED
        )
    except (MoodleUnreachable, SsoUnavailable) as exc:
        await _record_failure(
            worker,
            job_id=job_id,
            run_id=run_id,
            error=f"{ERROR_SYNC_FAILED}:{str(exc)[:120]}",
        )
    except Exception as exc:  # unexpected — never kill the tick loop
        logger.exception("syncjobs.run_crashed", job_id=job_id)
        await _record_failure(
            worker,
            job_id=job_id,
            run_id=run_id,
            error=f"{ERROR_SYNC_FAILED}:{type(exc).__name__}",
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
            account = await session.get(ExternalAccount, job.external_account_id)
            if account is not None:
                await mark_credentials_invalid(
                    session, account=account, user_id=job.user_id, error=error
                )
            else:
                job.status = SyncJobStatus.disabled.value
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
