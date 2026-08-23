"""Sync log tailing. `_log_table_ready` is rebound here on first use, so
it has to live in the same module as the endpoint that sets it."""

from __future__ import annotations
from fastapi import APIRouter, Depends, Query, Request
from ...db import get_pool

router = APIRouter(prefix="/api/moodle")


_log_table_ready = False
@router.get("/sync-logs")
async def sync_logs(
    student_id: str = Query(..., min_length=1),
    after_id: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
    pool=Depends(get_pool),
):
    global _log_table_ready
    async with pool.acquire() as conn:
        if not _log_table_ready:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS sync_log_entries (
                    id          BIGSERIAL PRIMARY KEY,
                    user_id     UUID NOT NULL,
                    device_id   UUID,
                    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
                    level       VARCHAR(8) NOT NULL DEFAULT 'INFO',
                    source      VARCHAR(32) NOT NULL,
                    message     TEXT NOT NULL,
                    detail      JSONB
                )
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_sync_log_entries_user
                    ON sync_log_entries (user_id, ts DESC)
            """)
            await conn.execute(
                "ALTER TABLE sync_log_entries ADD COLUMN IF NOT EXISTS device_id UUID"
            )
            _log_table_ready = True

        user = await conn.fetchrow(
            "SELECT id FROM users WHERE student_id = $1", student_id
        )
        if not user:
            return {"entries": [], "latest_id": after_id}

        rows = await conn.fetch(
            "SELECT e.id, e.ts, e.level, e.source, e.message, e.detail, "
            "d.client_device_id AS device_label, d.platform "
            "FROM sync_log_entries e "
            "LEFT JOIN user_devices d ON d.id = e.device_id "
            "WHERE e.user_id = $1 AND e.id > $2 "
            "ORDER BY e.id ASC LIMIT $3",
            user["id"], after_id, limit,
        )

    entries = [dict(r) for r in rows]
    latest_id = entries[-1]["id"] if entries else after_id
    return {"entries": entries, "latest_id": latest_id}
