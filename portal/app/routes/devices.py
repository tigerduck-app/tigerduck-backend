"""Registered-devices list, read directly from the v3 tables.

Replaces the old `/v2/devices` proxy: the portal now reads
`users` + `user_devices` + `device_push_tokens` over the tigerduck-db
network instead of calling the public backend API. Response shape is kept
identical to what the SPA already consumes (`DeviceRow`), so the v3 origin
is transparent to the frontend.

Field mapping (v2 device_registrations → v3):
  device_id        ← user_devices.id            (server UUID, stable key)
  user_id          ← users.student_id           (human-meaningful owner)
  device_class     ← platform (ios→iphone, ipados→ipad, else "")
  bundle_id/apns_env ← the device's push token row
  has_pts_token    ← a push_to_start token exists
  has_device_token ← a standard token exists
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from ..db import get_pool

router = APIRouter(prefix="/api/devices")

# One row per device. device_class is derived so the SPA's iPhone/iPad
# tabs (which match on device_class) keep working without a real column.
_LIST_SQL = """
WITH dev AS (
    SELECT
        ud.id::text                            AS device_id,
        COALESCE(u.student_id, u.id::text)     AS user_id,
        ud.platform                            AS platform,
        CASE ud.platform
            WHEN 'ios'    THEN 'iphone'
            WHEN 'ipados' THEN 'ipad'
            ELSE ''
        END                                    AS device_class,
        ud.server_push_enabled                 AS server_push_enabled,
        ud.created_at                          AS created_at,
        ud.updated_at                          AS updated_at,
        ud.client_device_id                    AS client_device_id,
        u.student_id                           AS student_id,
        (
            SELECT t.bundle_id FROM device_push_tokens t
            WHERE t.device_id = ud.id AND t.status = 'active'
            ORDER BY (t.token_kind = 'standard') DESC NULLS LAST, t.id
            LIMIT 1
        )                                      AS bundle_id,
        (
            SELECT t.environment FROM device_push_tokens t
            WHERE t.device_id = ud.id AND t.status = 'active'
            ORDER BY (t.token_kind = 'standard') DESC NULLS LAST, t.id
            LIMIT 1
        )                                      AS apns_env,
        EXISTS(
            SELECT 1 FROM device_push_tokens t
            WHERE t.device_id = ud.id
              AND t.token_kind = 'push_to_start' AND t.status = 'active'
        )                                      AS has_pts_token,
        EXISTS(
            SELECT 1 FROM device_push_tokens t
            WHERE t.device_id = ud.id
              AND t.token_kind = 'standard' AND t.status = 'active'
        )                                      AS has_device_token
    FROM user_devices ud
    JOIN users u ON u.id = ud.user_id
    WHERE ud.deleted_at IS NULL
)
SELECT * FROM dev
WHERE (
    $1::text IS NULL
    OR lower(client_device_id) LIKE $1
    OR lower(COALESCE(student_id, '')) LIKE $1
    OR device_id LIKE $1
)
ORDER BY updated_at DESC, device_id
LIMIT $2 OFFSET $3
"""

_COUNT_SQL = """
SELECT count(*)
FROM user_devices ud
JOIN users u ON u.id = ud.user_id
WHERE ud.deleted_at IS NULL
  AND (
    $1::text IS NULL
    OR lower(ud.client_device_id) LIKE $1
    OR lower(COALESCE(u.student_id, '')) LIKE $1
    OR ud.id::text LIKE $1
  )
"""


def _row_to_device(r) -> dict:
    return {
        "device_id": r["device_id"],
        "user_id": r["user_id"],
        "platform": r["platform"],
        "device_class": r["device_class"],
        "bundle_id": r["bundle_id"] or "",
        "apns_env": r["apns_env"] or "",
        "server_push_enabled": r["server_push_enabled"],
        "has_pts_token": r["has_pts_token"],
        "has_device_token": r["has_device_token"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
    }


@router.get("")
async def list_devices(
    pool=Depends(get_pool),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    search: str | None = Query(default=None, max_length=128),
) -> JSONResponse:
    needle = None
    if search and search.strip():
        needle = f"%{search.strip().lower()}%"
    async with pool.acquire() as conn:
        total = await conn.fetchval(_COUNT_SQL, needle)
        rows = await conn.fetch(_LIST_SQL, needle, limit, offset)
    return JSONResponse(
        content={
            "items": [_row_to_device(r) for r in rows],
            "total": int(total or 0),
        }
    )
