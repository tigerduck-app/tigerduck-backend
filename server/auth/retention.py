"""Prune auth_sessions rows `auth_session_retention_days` past their expiry.

A row matters to the refresh path until its token expires: before then, the
token presented again may be rotated again or must be refused as a reuse
(`_handle_revoked_reuse` in server/auth/service.py); after it, every answer is
a 401. Each device's first and last session of a day stay on in
`device_active_days`, which the Userbase dashboard reads for older days.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import Row, func, or_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.auth.models import AuthSession, DeviceActiveDay
from server.config import Settings
from server.db import INSERT_CHUNK_ROWS, delete_in_batches

logger = structlog.get_logger(__name__)


async def prune_expired_sessions(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> int:
    cutoff = datetime.now(UTC) - timedelta(days=settings.auth_session_retention_days)
    deleted = await delete_in_batches(
        session_factory,
        AuthSession,
        AuthSession.expires_at < cutoff,
        # A live row pointing at another is in a ring of unused rivals
        # (`_rotate_again`); cut out, a rival used later would not revoke the rest.
        or_(
            AuthSession.revoked_at.is_not(None),
            AuthSession.replaced_by_session_id.is_(None),
        ),
        returning=[AuthSession.device_id, AuthSession.created_at],
        then=_keep_active_days,
    )
    logger.info(
        "auth.sessions.retention.done",
        deleted=deleted,
        cutoff=cutoff.isoformat(),
        retention_days=settings.auth_session_retention_days,
    )
    return deleted


async def _keep_active_days(session: AsyncSession, rows: Sequence[Row[Any]]) -> None:
    days: dict[tuple[uuid.UUID, date], tuple[datetime, datetime]] = {}
    for row in rows:
        if row.device_id is None:
            continue
        key = (row.device_id, row.created_at.astimezone(UTC).date())
        first, last = days.get(key, (row.created_at, row.created_at))
        days[key] = (min(first, row.created_at), max(last, row.created_at))
    values = [
        {"device_id": device_id, "day": day, "first_at": first, "last_at": last}
        for (device_id, day), (first, last) in days.items()
    ]
    for start in range(0, len(values), INSERT_CHUNK_ROWS):
        stmt = insert(DeviceActiveDay).values(values[start : start + INSERT_CHUNK_ROWS])
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[DeviceActiveDay.device_id, DeviceActiveDay.day],
                set_={
                    "first_at": func.least(DeviceActiveDay.first_at, stmt.excluded.first_at),
                    "last_at": func.greatest(DeviceActiveDay.last_at, stmt.excluded.last_at),
                },
            )
        )
