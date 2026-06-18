"""Lightweight sync event logger — writes to sync_log_entries (auto-created)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS sync_log_entries (
    id          BIGSERIAL PRIMARY KEY,
    user_id     UUID NOT NULL,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    level       VARCHAR(8) NOT NULL DEFAULT 'INFO',
    source      VARCHAR(32) NOT NULL,
    message     TEXT NOT NULL,
    detail      JSONB
);
CREATE INDEX IF NOT EXISTS idx_sync_log_entries_user
    ON sync_log_entries (user_id, ts DESC);
"""


async def ensure_table(session: AsyncSession) -> None:
    await session.execute(__import__("sqlalchemy").text(CREATE_TABLE_SQL))
    await session.commit()


async def log_sync(
    session: AsyncSession,
    *,
    user_id: Any,
    source: str,
    message: str,
    level: str = "INFO",
    detail: dict | None = None,
) -> None:
    try:
        from sqlalchemy import text

        await session.execute(
            text(
                "INSERT INTO sync_log_entries (user_id, ts, level, source, message, detail) "
                "VALUES (:uid, :ts, :lvl, :src, :msg, :det)"
            ),
            {
                "uid": user_id,
                "ts": datetime.now(UTC),
                "lvl": level,
                "src": source,
                "msg": message,
                "det": json.dumps(detail) if detail else None,
            },
        )
    except Exception:
        logger.debug("sync log write failed (table may not exist yet)", exc_info=True)
