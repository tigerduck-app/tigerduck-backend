"""Prune the sync history: `sync_log_entries` and finished `sync_runs`.

Only the operator reads them: the portal's Moodle pages, the Moodle sync
dashboard and sql-exporter. The change feed the apps poll is
`user_change_log`, pruned in server/sync/retention.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.config import Settings
from server.db import delete_in_batches
from server.syncjobs.models import SyncLogEntry, SyncRun

logger = structlog.get_logger(__name__)


async def prune_sync_history(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> int:
    cutoff = datetime.now(UTC) - timedelta(days=settings.sync_history_retention_days)
    entries, stopped_early = await delete_in_batches(
        session_factory, SyncLogEntry, SyncLogEntry.ts < cutoff
    )
    runs = 0
    if not stopped_early:
        # A run still going has no finished_at, and the executor still writes it.
        runs, stopped_early = await delete_in_batches(
            session_factory, SyncRun, SyncRun.finished_at < cutoff
        )
    logger.info(
        "syncjobs.retention.done",
        log_entries=entries,
        runs=runs,
        stopped_early=stopped_early,
        cutoff=cutoff.isoformat(),
        retention_days=settings.sync_history_retention_days,
    )
    return entries + runs
