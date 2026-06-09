"""Changelog retention: purge entries past the retention window and advance
each user's `compacted_revision`.

Deletion is by per-user revision watermark, not raw `created_at`: we first
compute `max(revision)` among a user's expired rows, then delete
`revision <= watermark` while holding that user's `user_sync_state` row
lock (the same lock changelog appends take). That keeps the delete and the
`compacted_revision` bump atomic per user — a client paging concurrently
either reads rows before the commit or gets a clean 410 after it, never a
silent gap.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.config import Settings
from server.sync.models import UserChangeLog, UserSyncState

logger = structlog.get_logger(__name__)


async def purge_expired_changelog(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> int:
    """Delete changelog entries older than the retention window. Returns
    the number of rows deleted."""
    cutoff = datetime.now(UTC) - timedelta(
        days=settings.sync_changelog_retention_days
    )
    total_deleted = 0

    async with session_factory() as session:
        watermarks = (
            await session.execute(
                select(
                    UserChangeLog.user_id,
                    func.max(UserChangeLog.revision).label("watermark"),
                )
                .where(UserChangeLog.created_at < cutoff)
                .group_by(UserChangeLog.user_id)
            )
        ).all()

        for user_id, watermark in watermarks:
            # Same per-user lock the append path takes — serializes against
            # concurrent writes so compacted_revision can't leapfrog
            # current_revision.
            state = (
                await session.execute(
                    select(UserSyncState)
                    .where(UserSyncState.user_id == user_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            result = await session.execute(
                delete(UserChangeLog).where(
                    UserChangeLog.user_id == user_id,
                    UserChangeLog.revision <= watermark,
                )
            )
            total_deleted += result.rowcount or 0
            if state is not None:
                state.compacted_revision = max(
                    state.compacted_revision, watermark
                )
        await session.commit()

    logger.info(
        "sync.changelog.retention.done",
        deleted=total_deleted,
        users=len(watermarks),
        cutoff=cutoff.isoformat(),
        retention_days=settings.sync_changelog_retention_days,
    )
    return total_deleted
