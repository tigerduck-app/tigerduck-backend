"""Server-side sync job executor (sync-and-push spec §4).

Claim/execute split: one short transaction recovers stale locks, then a
tick claims due jobs one at a time, up to `sync_job_batch_size`, each as a
slot of `sync_job_concurrency` frees (`FOR UPDATE SKIP LOCKED`, joined to an
enabled+in-window policy, of users with a device syncing and no job
running, global running-count cap; none while the portal suspends sync or a
429 pauses the worker). Each job reads its token, fetches from Moodle with
no transaction open and stores the result in a transaction of its own, so
one failure can't poison the rest; an assignments job then probes
submission status in transactions of its own (`_refresh_submission_status`).
Every Moodle request of the worker waits its turn in one `RequestBudget`
(`sync_job_requests_per_minute`), for the school APIs behind our single
egress IP (tigerduck-backend#80).

Failure taxonomy → outcome:
* `CredentialInvalid`            → account invalidated, ALL user jobs
                                   disabled, reauth push queued. No retry.
* `MoodleRateLimited`            → retriable, backoff, last_error
                                   'school_rate_limited'; the whole
                                   worker pauses for the backoff's base.
* `MoodleUnreachable`/`SsoUnavailable`
                                 → retriable, backoff, last_error
                                   'sync_failed:<detail>'. An answer
                                   that lost every course
                                   (`MoodleNoCourseAccess`) first
                                   soft-deletes the rows of the courses
                                   it left out.
* `SubmissionStatusDbError`      → retriable, backoff, last_error
                                   'sync_failed:submission_status'; the
                                   assignment list sync it followed has
                                   already committed.
* unexpected                     → retriable, backoff, last_error
                                   'sync_failed:<exception type>'.
Past max_attempts, the school's errors (`MoodleRateLimited`,
`MoodleUnreachable`, `SsoUnavailable`) put the job back on its policy
interval with its attempts and priority reset: they end without us, and a
failed job waits for an operator, since no released app calls
`POST /v3/sync-jobs/run-now` and re-login revives only a 'disabled' job.
Our own errors (the submission-status database error, an unexpected
exception, a stale lock) can come back on every run, so they leave the job
'failed' until run-now or the portal's retry revives it.
"""

from __future__ import annotations

import asyncio
import os
import socket
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from server.auth.crypto import CredentialCipher
from server.auth.models import (
    DevicePushToken,
    ExternalAccount,
    ExternalAccountCredential,
    PushTokenKind,
    PushTokenStatus,
    UserDevice,
)
from server.auth.service import try_store_display_name
from server.config import Settings
from server.db import session_scope
from server.syncjobs.assignments import apply_fetched_assignments
from server.syncjobs.availability import is_moodle_available, manual_suspend_until
from server.syncjobs.budget import RequestBudget
from server.syncjobs.courses import apply_fetched_courses
from server.syncjobs.credentials import (
    CredentialInvalid,
    load_credential,
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
    FetchedAssignment,
    MoodleClientError,
    MoodleNoCourseAccess,
    MoodleRateLimited,
    MoodleTokenInvalid,
    MoodleUnreachable,
    SsoUnavailable,
    _without_token,
)
from server.syncjobs.submissions import (
    apply_extensions,
    apply_submission_status,
    select_assignment_ids,
)
from server.push.submission_cancel import cancel_for_submitted
from server.push.sync_trigger import enqueue_sync_trigger

logger = structlog.get_logger(__name__)

ERROR_CREDENTIAL_INVALID = "credential_invalid"
ERROR_SCHOOL_RATE_LIMITED = "school_rate_limited"
ERROR_SYNC_FAILED = "sync_failed"


class SubmissionStatusDbError(Exception):
    """A database error while refreshing submission status, raised once the
    assignment list sync has committed. It fails the run, so the job backs
    off and the probe is retried within minutes rather than at the next
    policy interval."""

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
    # Shared by every job in flight; the fetchers send through its hooks.
    budget: RequestBudget


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
    """One scheduler tick: recover stale locks, then claim due jobs one at a
    time, up to `sync_job_batch_size`, and run `sync_job_concurrency` of them
    at once. Returns the number of jobs started."""
    if _in_maintenance_window(worker.settings.sync_maintenance_window):
        logger.info(
            "syncjobs.maintenance_window",
            window=worker.settings.sync_maintenance_window,
        )
        return 0
    await _recover_stale_jobs(worker)
    slots = asyncio.Semaphore(worker.settings.sync_job_concurrency)
    running: list[asyncio.Task[None]] = []
    try:
        while len(running) < worker.settings.sync_job_batch_size:
            await slots.acquire()
            claimed = await _claim_next(worker)
            if claimed is None:
                slots.release()
                break
            running.append(
                asyncio.create_task(_execute_in_slot(worker, claimed, slots))
            )
    except asyncio.CancelledError:
        for task in running:
            task.cancel()
        raise
    finally:
        # A claim that failed leaves the jobs already started to finish.
        await asyncio.gather(*running, return_exceptions=True)
    return len(running)


async def _claim_next(worker: SyncWorker) -> tuple[int, int] | None:
    """The next job to start, or None while a 429 pauses the worker, an
    operator suspends sync, or no job is due."""
    if worker.budget.paused or await _suspended(worker):
        return None
    return await _claim_due_job(worker)


async def _execute_in_slot(
    worker: SyncWorker, claimed: tuple[int, int], slots: asyncio.Semaphore
) -> None:
    try:
        await _execute_job(worker, job_id=claimed[0], run_id=claimed[1])
    finally:
        slots.release()


async def _suspended(worker: SyncWorker) -> bool:
    """Whether an operator suspended sync in the portal (tigerduck-backend#86)."""
    async with session_scope(worker.session_factory) as session:
        until = await manual_suspend_until(session, datetime.now(UTC))
    if until is not None:
        logger.info("syncjobs.suspended", until=until.isoformat())
    return until is not None


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


def _has_syncing_device(settings: Settings, now: datetime) -> sa.Exists:
    """A fetch serves only a device with TigerSync on that is still in use:
    seen lately, or reached by a push lately. Signing out deletes the device.
    Until one is back, the job waits unclaimed (tigerduck-backend#85)."""
    return sa.exists().where(
        UserDevice.user_id == SyncJob.user_id,
        UserDevice.deleted_at.is_(None),
        UserDevice.cloud_sync_enabled.is_(True),
        sa.or_(
            UserDevice.last_seen_at
            > now - timedelta(days=settings.sync_job_device_active_days),
            sa.exists().where(
                DevicePushToken.device_id == UserDevice.id,
                DevicePushToken.token_kind == PushTokenKind.standard.value,
                DevicePushToken.status == PushTokenStatus.active.value,
                DevicePushToken.last_success_at
                > now - timedelta(days=settings.sync_job_device_push_days),
            ),
        ),
    )


async def _claim_due_job(worker: SyncWorker) -> tuple[int, int] | None:
    """Claim the next due job and open its run: (job id, run id)."""
    now = datetime.now(UTC)
    busy = aliased(SyncJob)
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
        if running >= worker.settings.sync_job_global_concurrency:
            return None

        job = (
            (
                await session.execute(
                    select(SyncJob)
                    .join(SyncPolicy, SyncPolicy.job_type == SyncJob.job_type)
                    .where(
                        SyncJob.status == SyncJobStatus.pending.value,
                        SyncJob.run_after <= now,
                        _has_syncing_device(worker.settings, now),
                        # One job of a user at a time: both write the user's
                        # rows, and a rejected token disables all of them.
                        ~sa.exists().where(
                            busy.user_id == SyncJob.user_id,
                            busy.status == SyncJobStatus.running.value,
                        ),
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
                    .limit(1)
                    .with_for_update(skip_locked=True, of=SyncJob)
                )
            )
            .scalars()
            .one_or_none()
        )
        if job is None:
            return None
        job.status = SyncJobStatus.running.value
        job.locked_by = worker.worker_id
        job.locked_at = now
        run = SyncRun(sync_job_id=job.id, user_id=job.user_id)
        session.add(run)
        await session.flush()
        return job.id, run.id


async def _execute_job(worker: SyncWorker, *, job_id: int, run_id: int) -> None:
    """Run one claimed job: read its token, fetch from Moodle with no
    transaction open, then store the result under the job's row lock again,
    re-checked, since the claim may have been taken back meanwhile."""
    credential_nonce = None
    try:
        started = await _start_run(worker, job_id=job_id, run_id=run_id)
        if started is None:
            return
        job_type, user_id, token, credential_nonce = started
        now = datetime.now(UTC)
        names: list[str] = []
        probe_submissions = job_type != SyncJobType.ntust_courses.value
        if not probe_submissions:
            try:
                fetched = await worker.course_fetcher.fetch_courses(
                    token=token, on_fullname=names.append
                )
            except MoodleTokenInvalid:
                raise CredentialInvalid("moodle_token_invalid")
        else:
            try:
                fetched = await worker.fetcher.fetch_assignments(token=token)
            except MoodleTokenInvalid:
                raise CredentialInvalid("moodle_token_invalid")
            except MoodleNoCourseAccess as exc:
                async with session_scope(worker.session_factory) as session:
                    owned = await _lock_owned_job(
                        session, worker, job_id=job_id, run_id=run_id
                    )
                    if owned is None:
                        return
                    await _drop_courses_left_out(
                        session,
                        run=owned[1],
                        user_id=user_id,
                        refused_course_ids=exc.refused_course_ids,
                        now=now,
                    )
                raise

        from .log_entries import log_sync

        async with session_scope(worker.session_factory) as session:
            owned = await _lock_owned_job(
                session, worker, job_id=job_id, run_id=run_id
            )
            if owned is None:
                return
            job, run = owned
            if probe_submissions:
                await log_sync(session, user_id=user_id, source="executor",
                               message=f"Fetched {len(fetched)} assignments from Moodle")
                stats = await apply_fetched_assignments(
                    session, user_id=user_id, fetched=fetched, now=now
                )
            else:
                await log_sync(session, user_id=user_id, source="executor",
                               message=f"Fetched {len(fetched)} courses from Moodle")
                stats = await apply_fetched_courses(
                    session, user_id=user_id, fetched=fetched, now=now
                )

            if run is not None:
                run.fetched_count = stats.fetched_count
                run.changed_count = stats.changed_count

            if stats.changed_count > 0:
                await enqueue_sync_trigger(session, user_id)

            if not probe_submissions:
                policy = (
                    await session.execute(
                        select(SyncPolicy).where(SyncPolicy.job_type == job.job_type)
                    )
                ).scalar_one_or_none()
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
                    worker,
                    user_id=user_id,
                    token=token,
                    now=now,
                    listed={a.moodle_assignment_id: a for a in fetched},
                    recheck_ids=stats.recheck_ids,
                    on_fullname=names.append,
                )
            except MoodleTokenInvalid:
                raise CredentialInvalid("moodle_token_invalid")
            await _finish_after_probe(
                worker, job_id=job_id, run_id=run_id, stats=stats, now=now
            )
        # Last, once the fetched data is stored: a user who does not sign in
        # again still gets a name, and a failed write costs nothing else.
        await try_store_display_name(
            worker.session_factory, user_id, names[-1] if names else None
        )
    except CredentialInvalid as exc:
        await _handle_moodle_failure(
            worker, job_id=job_id, run_id=run_id,
            error=ERROR_CREDENTIAL_INVALID, detail=exc.reason,
            is_token_invalid=True, credential_nonce=credential_nonce,
        )
    except MoodleRateLimited:
        pause = worker.settings.sync_job_backoff_base_seconds
        worker.budget.pause(pause)
        logger.warning("syncjobs.rate_limited_pause", job_id=job_id, seconds=pause)
        await _record_failure(
            worker, job_id=job_id, run_id=run_id, error=ERROR_SCHOOL_RATE_LIMITED,
            keep_retrying=True,
        )
    except (MoodleUnreachable, SsoUnavailable) as exc:
        await _handle_moodle_failure(
            worker, job_id=job_id, run_id=run_id,
            error=f"{ERROR_SYNC_FAILED}:{str(exc)[:120]}",
            is_token_invalid=False,
            keep_retrying=True,
        )
    except SubmissionStatusDbError:
        # Logged where it happened, under its own event, so not as a crash.
        await _record_failure(
            worker, job_id=job_id, run_id=run_id,
            error=f"{ERROR_SYNC_FAILED}:submission_status",
        )
    except Exception as exc:  # unexpected — never kill the tick loop
        logger.exception("syncjobs.run_crashed", job_id=job_id)
        await _record_failure(
            worker,
            job_id=job_id,
            run_id=run_id,
            error=f"{ERROR_SYNC_FAILED}:{type(exc).__name__}",
        )


def _owned(job: SyncJob | None, worker: SyncWorker) -> bool:
    """Whether `job` is still this worker's run: stale recovery may have
    taken it back, and another worker claimed it, while Moodle answered."""
    return (
        job is not None
        and job.status == SyncJobStatus.running.value
        and job.locked_by == worker.worker_id
    )


async def _lock_owned_job(
    session: AsyncSession, worker: SyncWorker, *, job_id: int, run_id: int
) -> tuple[SyncJob, SyncRun | None] | None:
    """Lock the job and its run, or None if this worker no longer holds it:
    stale-recovered, and possibly reclaimed by another worker, since the
    claim. Never double-execute."""
    job = (
        await session.execute(
            select(SyncJob).where(SyncJob.id == job_id).with_for_update()
        )
    ).scalar_one_or_none()
    run = await session.get(SyncRun, run_id)
    if not _owned(job, worker):
        if run is not None and run.status == SyncRunStatus.running.value:
            run.status = SyncRunStatus.cancelled.value
            run.finished_at = datetime.now(UTC)
            run.error = "reclaimed_before_execution"
        logger.warning(
            "syncjobs.job_reclaimed", job_id=job_id, worker=worker.worker_id
        )
        return None
    return job, run


async def _start_run(
    worker: SyncWorker, *, job_id: int, run_id: int
) -> tuple[str, uuid.UUID, str, bytes] | None:
    """The job's type, user and Moodle token, and the nonce of the stored
    credential it came from, read and logged in a transaction of its own;
    None if the job is no longer this worker's."""
    from .log_entries import log_sync

    async with session_scope(worker.session_factory) as session:
        owned = await _lock_owned_job(session, worker, job_id=job_id, run_id=run_id)
        if owned is None:
            return None
        job, _run = owned
        _account, credential, blob = await load_credential(
            session, worker.cipher, external_account_id=job.external_account_id
        )
        # One row read gives both the token and the nonce a rejection compares.
        nonce = credential.nonce
        token = (blob.get("token_cache") or {}).get("moodle_token")
        if not isinstance(token, str) or not token:
            raise CredentialInvalid("moodle_token_missing")
        await log_sync(session, user_id=job.user_id, source="executor",
                       message=f"Job started: {job.job_type}",
                       detail={"job_id": job.id, "run_id": run_id})
        return job.job_type, job.user_id, token, nonce


async def _drop_courses_left_out(
    session: AsyncSession,
    *,
    run: SyncRun | None,
    user_id,
    refused_course_ids: frozenset[int],
    now: datetime,
) -> None:
    """Soft-delete the rows of the courses an answer that lost every course
    left out, a TA's course among them, and keep the refused courses' rows.

    Committed here, with the run's counts; the run then fails like any other
    unreachable Moodle.
    """
    from .log_entries import log_sync

    stats = await apply_fetched_assignments(
        session,
        user_id=user_id,
        fetched=[],
        now=now,
        refused_course_ids=refused_course_ids,
    )
    if run is not None:
        run.fetched_count = stats.fetched_count
        run.changed_count = stats.changed_count
    if stats.changed_count > 0:
        await enqueue_sync_trigger(session, user_id)
    await log_sync(
        session,
        user_id=user_id,
        source="executor",
        message=(
            f"Moodle refused {len(refused_course_ids)} courses and listed none; "
            f"deleted {stats.changed_count} assignments of the courses it left out"
        ),
        detail={
            "refused_course_ids": sorted(refused_course_ids),
            "deleted": stats.changed_count,
        },
    )
    await session.commit()


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
    listed: dict[int, FetchedAssignment],
    recheck_ids: frozenset[int],
    on_fullname: Callable[[str], None],
) -> None:
    """Best-effort probe of assignments near their due date, or with an
    extension, and of those whose due date the list sync just moved back
    (`recheck_ids`), whose answer may move it forward again
    (`select_assignment_ids`).

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
    The same transaction writes each answer's extension and the dates it
    gives with the ones `listed` by this run's list sync
    (`apply_extensions`), and enqueues a sync trigger of its own when
    anything changed, so the user's other devices fetch the flip or the new
    dates (both logged to the changelog) instead of waiting for their next
    unrelated sync.

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
    * A database error from any of the database calls
      (`select_assignment_ids`, `apply_submission_status`,
      `cancel_for_submitted`, `apply_extensions`) is not a probe failure
      and is never logged as one. Its own transaction rolls back, and it is
      logged under a separate event so it cannot be mistaken for routine
      Moodle noise. It then fails
      the run as `SubmissionStatusDbError`: recorded as a success, the next
      probe would be a whole policy interval away, and until then an
      assignment already submitted keeps its reminder queued and its Live
      Activity counting down.
    """
    settings = worker.settings
    try:
        async with session_scope(worker.session_factory) as session:
            assignment_ids = await select_assignment_ids(
                session,
                user_id=user_id,
                now=now,
                window_hours=settings.submission_status_window_hours,
                also=recheck_ids,
            )
        if not assignment_ids:
            return
        try:
            fetched = await worker.fetcher.fetch_submission_status(
                token=token,
                assignment_ids=assignment_ids,
                max_concurrency=settings.submission_status_max_concurrency,
                on_fullname=on_fullname,
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
            moved = await apply_extensions(
                session, user_id=user_id, fetched=fetched, listed=listed
            )
            if changed_ids or moved:
                await enqueue_sync_trigger(session, user_id, source="submissions")
    except (MoodleTokenInvalid, MoodleRateLimited, MoodleUnreachable):
        raise
    except Exception as exc:
        logger.error("syncjobs.submissions.refresh_db_error", exc_info=True)
        raise SubmissionStatusDbError from exc


async def _handle_moodle_failure(
    worker: SyncWorker,
    *,
    job_id: int,
    run_id: int,
    error: str,
    detail: str | None = None,
    is_token_invalid: bool,
    keep_retrying: bool = False,
    credential_nonce: bytes | None = None,
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
            if _owned(job, worker):
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
            credential_nonce=credential_nonce,
        )
    else:
        await _record_failure(
            worker, job_id=job_id, run_id=run_id,
            error=error, keep_retrying=keep_retrying,
        )


async def _rejected_token_still_stored(
    session: AsyncSession, external_account_id: int, credential_nonce: bytes | None
) -> bool:
    """Whether the credential stored under `credential_nonce` is still the
    account's: the app may have stored a fresh token while Moodle rejected
    the old one. The account row is locked before the credential's, the
    order PATCH /v3/auth/credentials writes them in."""
    await session.get(ExternalAccount, external_account_id, with_for_update=True)
    if credential_nonce is None:
        return True
    credential = await session.get(
        ExternalAccountCredential,
        external_account_id,
        with_for_update=True,
        populate_existing=True,
    )
    return credential is not None and credential.nonce == credential_nonce


async def _record_failure(
    worker: SyncWorker,
    *,
    job_id: int,
    run_id: int,
    error: str,
    disable: bool = False,
    detail: str | None = None,
    keep_retrying: bool = False,
    credential_nonce: bytes | None = None,
) -> None:
    """Record a failure in a FRESH session — the work session may have
    rolled back (or be poisoned by a DB error).

    `disable` invalidates the account's credential only while the rejected
    token, the one stored under `credential_nonce`, is still the stored
    one; a token the app stored since is left alone and the job retried.

    With `keep_retrying`, a job past its max_attempts is not left 'failed'
    but runs again after its policy interval, with its attempts and
    priority reset as after a success: a later transient error gets its
    full retries, and a run-now's priority does not outlast it."""
    async with session_scope(worker.session_factory) as session:
        job = (
            await session.execute(
                select(SyncJob).where(SyncJob.id == job_id).with_for_update()
            )
        ).scalar_one_or_none()
        if not _owned(job, worker):
            logger.warning(
                "syncjobs.job_reclaimed", job_id=job_id, worker=worker.worker_id
            )
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

        if disable and await _rejected_token_still_stored(
            session, job.external_account_id, credential_nonce
        ):
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
        if disable:
            logger.info("syncjobs.rejected_token_replaced", job_id=job_id)
            keep_retrying = True

        job.attempts += 1
        if job.attempts >= job.max_attempts and keep_retrying:
            policy = (
                await session.execute(
                    select(SyncPolicy).where(SyncPolicy.job_type == job.job_type)
                )
            ).scalar_one_or_none()
            interval = (
                policy.default_interval_seconds
                if policy
                else _DEFAULT_INTERVAL_SECONDS
            )
            logger.warning(
                "syncjobs.run_retry_at_interval",
                job_id=job_id,
                attempts=job.attempts,
                interval_seconds=interval,
                error=error,
            )
            job.status = SyncJobStatus.pending.value
            job.run_after = now + timedelta(seconds=interval)
            job.attempts = 0
            job.priority = policy.priority if policy else _DEFAULT_PRIORITY
        elif job.attempts >= job.max_attempts:
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
