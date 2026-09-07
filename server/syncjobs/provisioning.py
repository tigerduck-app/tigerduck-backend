"""Create sync_jobs at login (migration plan Phase 3, handoff decision:
login-time upsert, not backfill).

Jobs are created for every job type the executor can actually run
(`HANDLED_JOB_TYPES`); the executor filters by policy enabled/active
window at claim time, so admin policy toggles affect all users without
backfill. A `disabled` job is revived on re-login — the user just stored
fresh credentials. Healthy jobs are left untouched.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from server.syncjobs.models import SyncJob, SyncJobStatus, SyncJobType, SyncPolicy

# Phase 3 implements the Moodle assignment fetcher only. ntust_courses /
# calendar / grades policies exist but have no per-user jobs until their
# fetchers land.
HANDLED_JOB_TYPES: tuple[str, ...] = (
    SyncJobType.moodle_assignments.value,
    SyncJobType.ntust_courses.value,
)


async def ensure_sync_jobs(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    external_account_id: int,
) -> None:
    now = datetime.now(UTC)
    policies = {
        p.job_type: p
        for p in (await session.execute(select(SyncPolicy))).scalars()
    }
    values = [
        {
            "user_id": user_id,
            "external_account_id": external_account_id,
            "job_type": job_type,
            "run_after": now,
            "priority": (
                policies[job_type].priority if job_type in policies else 100
            ),
            "max_attempts": (
                policies[job_type].max_attempts if job_type in policies else 3
            ),
        }
        for job_type in HANDLED_JOB_TYPES
    ]
    await session.execute(
        pg_insert(SyncJob)
        .values(values)
        .on_conflict_do_update(
            index_elements=["user_id", "job_type"],
            set_={
                "status": SyncJobStatus.pending.value,
                "attempts": 0,
                "run_after": now,
                "locked_by": None,
                "locked_at": None,
                "last_error": None,
                "external_account_id": external_account_id,
            },
            where=(SyncJob.status == SyncJobStatus.disabled.value),
        )
    )
