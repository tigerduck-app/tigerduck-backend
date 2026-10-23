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

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import JSONResponse

from ..audit import audit
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
        ud.app_version                         AS app_version,
        ud.os_version                          AS os_version,
        ud.cloud_sync_enabled                  AS cloud_sync_enabled,
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
              AND t.provider = 'apns'
        )                                      AS has_device_token,
        EXISTS(
            SELECT 1 FROM device_push_tokens t
            WHERE t.device_id = ud.id
              AND t.token_kind = 'standard' AND t.status = 'active'
              AND t.provider = 'fcm'
        )                                      AS has_fcm_token
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
        "has_fcm_token": r["has_fcm_token"],
        "app_version": r["app_version"] or None,
        "os_version": r["os_version"] or None,
        "cloud_sync_enabled": r["cloud_sync_enabled"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
    }


@router.get("")
async def list_devices(
    request: Request,
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
        await audit(conn, request, "user.list", count=len(rows))
    return JSONResponse(
        content={
            "items": [_row_to_device(r) for r in rows],
            "total": int(total or 0),
        }
    )


@router.post("/deregister")
async def deregister_devices(
    request: Request,
    pool=Depends(get_pool),
    body: dict = Body(default={}),
) -> JSONResponse:
    device_ids = body.get("device_ids", [])
    if not device_ids or not isinstance(device_ids, list):
        return JSONResponse(content={"deleted": 0})
    async with pool.acquire() as conn:
        # Atomic: without the explicit transaction each statement
        # autocommits, and a crash between them would leave a soft-deleted
        # device with still-active push tokens.
        async with conn.transaction():
            deleted = await conn.fetch("""
                UPDATE user_devices
                SET deleted_at = now()
                WHERE id::text = ANY($1::text[])
                  AND deleted_at IS NULL
                RETURNING id, user_id
            """, device_ids)
            count = len(deleted)
            for d in deleted:
                await audit(
                    conn, request, "device.deregister",
                    user_id=d["user_id"], device_id=d["id"],
                )
            if count > 0:
                await conn.execute("""
                    UPDATE device_push_tokens
                    SET status = 'invalidated'
                    WHERE device_id::text = ANY($1::text[])
                      AND status = 'active'
                """, device_ids)
                # As server.auth.service.forget_credentials_once_signed_out
                # does for the app's own sign-out.
                await conn.execute("""
                    DELETE FROM external_account_credentials c
                    USING external_accounts a
                    WHERE c.external_account_id = a.id
                      AND a.user_id = ANY($1::uuid[])
                      AND NOT EXISTS (
                          SELECT 1 FROM user_devices d
                          WHERE d.user_id = a.user_id AND d.deleted_at IS NULL
                      )
                """, list({d["user_id"] for d in deleted}))
    return JSONResponse(content={"deleted": count})
