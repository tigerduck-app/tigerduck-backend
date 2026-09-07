"""Prune terminal push_jobs and their cascade-deleted push_deliveries."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.auth.models import PushJob
from server.config import Settings

logger = structlog.get_logger(__name__)

_TERMINAL_STATES = ("sent", "partial_failed", "failed", "cancelled")


async def prune_terminal_push_jobs(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(
        days=settings.push_job_retention_days
    )
    async with session_factory() as session:
        result = await session.execute(
            delete(PushJob).where(
                PushJob.status.in_(_TERMINAL_STATES),
                PushJob.updated_at < cutoff,
            )
        )
        await session.commit()
    deleted = result.rowcount or 0
    logger.info(
        "push_jobs.retention.done",
        deleted=deleted,
        cutoff=cutoff.isoformat(),
        retention_days=settings.push_job_retention_days,
    )
    return deleted
