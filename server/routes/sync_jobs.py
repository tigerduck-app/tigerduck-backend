"""/v3/sync-jobs — pull-to-refresh; /v3/admin/sync-policies — dashboard.

Pull-to-refresh never touches school APIs inline: it re-prioritizes the
user's existing sync job (priority=1, run_after=now) and the 30s executor
tick picks it up. The per-user cooldown lives in `sync_jobs.cursor`
(JSONB) so it survives restarts and works across instances.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

import structlog
from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import select

from server.auth.dependencies import CurrentAuthDep
from server.auth.models import ExternalAccount
from server.auth.service import PROVIDER_NTUST_SSO
from server.db import SessionDep
from server.syncjobs.models import SyncJob, SyncJobStatus
from server.syncjobs.provisioning import ensure_sync_jobs

router = APIRouter(prefix="/sync-jobs", tags=["sync-jobs"])
logger = structlog.get_logger(__name__)

HandledJobType = Literal["moodle_assignments"]

ERROR_CODES = ("credential_invalid", "school_rate_limited", "sync_failed")


def _last_error_code(job: SyncJob) -> str | None:
    if job.status == SyncJobStatus.disabled.value:
        return "credential_invalid"
    if job.last_error is None:
        return None
    for code in ERROR_CODES:
        if job.last_error.startswith(code):
            return code
    return "sync_failed"


def _status_payload(job: SyncJob, *, queued: bool) -> dict:
    return {
        "job_type": job.job_type,
        "status": job.status,
        "priority": job.priority,
        "run_after": job.run_after.isoformat(),
        "attempts": job.attempts,
        "last_success_at": (
            job.last_success_at.isoformat() if job.last_success_at else None
        ),
        "last_failure_at": (
            job.last_failure_at.isoformat() if job.last_failure_at else None
        ),
        "last_error_code": _last_error_code(job),
        "queued": queued,
    }


@router.post("/run-now")
async def run_now(
    auth: CurrentAuthDep,
    session: SessionDep,
    request: Request,
    job_type: HandledJobType = Query(),
):
    settings = request.app.state.settings
    now = datetime.now(UTC)

    job = (
        await session.execute(
            select(SyncJob)
            .where(SyncJob.user_id == auth.user_id, SyncJob.job_type == job_type)
            .with_for_update()
        )
    ).scalar_one_or_none()

    if job is None:
        account = (
            await session.execute(
                select(ExternalAccount).where(
                    ExternalAccount.user_id == auth.user_id,
                    ExternalAccount.provider == PROVIDER_NTUST_SSO,
                )
            )
        ).scalar_one_or_none()
        if account is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "sync_not_provisioned"},
            )
        await ensure_sync_jobs(
            session, user_id=auth.user_id, external_account_id=account.id
        )
        job = (
            await session.execute(
                select(SyncJob)
                .where(
                    SyncJob.user_id == auth.user_id,
                    SyncJob.job_type == job_type,
                )
                .with_for_update()
            )
        ).scalar_one()

    if job.status == SyncJobStatus.disabled.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "credential_invalid"},
        )

    if job.status == SyncJobStatus.running.value:
        return _status_payload(job, queued=False)
    if (
        job.status == SyncJobStatus.pending.value
        and job.priority <= 1
        and job.run_after <= now
    ):
        # Already queued at high priority — don't burn the cooldown.
        return _status_payload(job, queued=False)

    cooldown = timedelta(seconds=settings.sync_job_manual_cooldown_seconds)
    requested_raw = (job.cursor or {}).get("manual_requested_at")
    if isinstance(requested_raw, str):
        try:
            requested_at = datetime.fromisoformat(requested_raw)
        except ValueError:
            requested_at = None
        if requested_at is not None and now - requested_at < cooldown:
            retry_after = int((requested_at + cooldown - now).total_seconds())
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error": "cooldown",
                    "retry_after_seconds": max(retry_after, 1),
                },
            )

    if job.status == SyncJobStatus.failed.value:
        job.attempts = 0  # manual revive
    job.status = SyncJobStatus.pending.value
    job.priority = 1
    job.run_after = now
    # Reassign (not mutate) so SQLAlchemy detects the JSONB change.
    job.cursor = {**(job.cursor or {}), "manual_requested_at": now.isoformat()}
    logger.info(
        "syncjobs.run_now",
        user_id=str(auth.user_id),
        job_type=job_type,
    )
    return _status_payload(job, queued=True)
