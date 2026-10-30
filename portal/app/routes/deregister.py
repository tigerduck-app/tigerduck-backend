"""Deregister (hard-delete) a student and all associated data.

Looks up the user by `student_id`, collects a deletion report
(counts + device IDs), then DELETEs everything. The report is
returned so the frontend can offer it as a downloadable file.
`lookup` finds the student by id or name and `preview` shows the same
report before anything is deleted.
"""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, UTC

from asyncpg.exceptions import DeadlockDetectedError
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..audit import audit
from ..db import get_pool
from ..student_ids import student_id_key

router = APIRouter(prefix="/api/deregister")
logger = logging.getLogger(__name__)

# The sync executor locks a job before the users row its run needs, the other
# way round from here, so Postgres may abort this as a deadlock (server/AGENTS.md).
_ATTEMPTS = 3


class DeregisterRequest(BaseModel):
    student_id: str


def _iso(ts) -> str | None:
    return ts.isoformat() if ts else None


# A student id matches whole, as login keys it ($2); anything else is part of a name.
_LOOKUP_SQL = """
WITH found AS (
    SELECT id FROM users WHERE student_id = $2
), named AS (
    SELECT id FROM users
    WHERE NOT EXISTS (SELECT 1 FROM found)
      AND strpos(lower(display_name), lower($1)) > 0
)
SELECT u.student_id, u.display_name, u.created_at,
       count(d.id) AS devices, max(d.last_seen_at) AS last_active
FROM users u
LEFT JOIN user_devices d ON d.user_id = u.id AND d.deleted_at IS NULL
WHERE u.id IN (SELECT id FROM found UNION ALL SELECT id FROM named)
GROUP BY u.id
ORDER BY max(d.last_seen_at) DESC NULLS LAST, u.student_id
LIMIT 50
"""


@router.get("/lookup")
async def lookup_students(
    request: Request,
    pool=Depends(get_pool),
    q: str = Query(min_length=1, max_length=64),
) -> JSONResponse:
    # A blank term is part of every name.
    if not q.strip():
        return JSONResponse(content={"students": []})
    async with pool.acquire() as conn:
        rows = await conn.fetch(_LOOKUP_SQL, q.strip(), student_id_key(q))
        await audit(conn, request, "user.list", count=len(rows))
    return JSONResponse(
        content={
            "students": [
                {
                    "student_id": r["student_id"],
                    "display_name": r["display_name"],
                    "devices": r["devices"],
                    "last_active": _iso(r["last_active"]),
                    "joined": _iso(r["created_at"]),
                }
                for r in rows
            ]
        }
    )


@router.get("/preview")
async def preview_student(
    request: Request,
    pool=Depends(get_pool),
    student_id: str = Query(min_length=1, max_length=64),
) -> JSONResponse:
    """What `deregister_student` would delete, in its report's shape."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, student_id, display_name, created_at FROM users"
            " WHERE student_id = $1",
            student_id_key(student_id),
        )
        if row is None:
            return JSONResponse(content={"error": "user_not_found"}, status_code=404)
        device_list, counts = await _snapshot(conn, row["id"])
        await audit(conn, request, "user.view", user_id=row["id"])
    seen = [d["last_seen_at"] for d in device_list if d["last_seen_at"]]
    return JSONResponse(
        content={
            "student_id": row["student_id"],
            "display_name": row["display_name"],
            "account_created_at": _iso(row["created_at"]),
            "last_active": max(seen, default=None),
            "devices": device_list,
            "deleted_counts": counts,
        }
    )


@router.post("")
async def deregister_student(
    body: DeregisterRequest,
    request: Request,
    pool=Depends(get_pool),
) -> JSONResponse:
    student_id = student_id_key(body.student_id)
    if not student_id:
        return JSONResponse(
            content={"error": "student_id_required"}, status_code=400
        )

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, student_id, created_at FROM users WHERE student_id = $1",
            student_id,
        )
        if row is None:
            return JSONResponse(
                content={"error": "user_not_found"}, status_code=404
            )
        user_id = row["id"]

        for attempt in range(1, _ATTEMPTS + 1):
            try:
                async with conn.transaction():
                    device_list, counts = await _delete_account(conn, user_id)
                    await audit(
                        conn, request, "user.deregister",
                        user_id=user_id, devices=len(device_list),
                    )
                break
            except DeadlockDetectedError:
                if attempt == _ATTEMPTS:
                    raise
                logger.warning("deregister aborted as a deadlock, attempt %d", attempt)
                # Jittered, so the transaction it lost to can finish first.
                await asyncio.sleep(random.uniform(0.1, 0.5))

    report = {
        "deleted": True,
        "timestamp": datetime.now(UTC).isoformat(),
        "student_id": student_id,
        "user_id": str(user_id),
        "account_created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "devices": device_list,
        "deleted_counts": counts,
    }
    return JSONResponse(content=report)


async def _delete_account(conn, user_id) -> tuple[list[dict], dict]:
    """Delete the account and return `_snapshot` of what went."""
    # The order sign-in takes (`lock_accounts`), then the account the retention
    # purges lock (`delete_in_batches`), before any row of either: see server/AGENTS.md.
    await conn.execute(
        "SELECT 1 FROM external_accounts WHERE user_id = $1 ORDER BY id FOR UPDATE",
        user_id,
    )
    await conn.execute("SELECT 1 FROM users WHERE id = $1 FOR UPDATE", user_id)
    snapshot = await _snapshot(conn, user_id)
    await conn.execute(
        """DELETE FROM external_account_credentials
           WHERE external_account_id IN (
               SELECT id FROM external_accounts WHERE user_id = $1
           )""",
        user_id,
    )
    await conn.execute("DELETE FROM external_accounts WHERE user_id = $1", user_id)
    await conn.execute("DELETE FROM users WHERE id = $1", user_id)
    return snapshot


async def _snapshot(conn, user_id) -> tuple[list[dict], dict]:
    """The student's registered devices and the rows a deregister deletes, per table."""
    devices = await conn.fetch(
        "SELECT id, client_device_id, platform, app_version, os_version, last_seen_at "
        "FROM user_devices WHERE user_id = $1 AND deleted_at IS NULL",
        user_id,
    )
    device_list = [
        {
            "device_id": str(d["id"]),
            "client_device_id": d["client_device_id"],
            "platform": d["platform"],
            "app_version": d["app_version"],
            "os_version": d["os_version"],
            "last_seen_at": d["last_seen_at"].isoformat() if d["last_seen_at"] else None,
        }
        for d in devices
    ]

    counts = {}
    for table in [
        "user_courses",
        "user_course_overrides",
        "user_assignments",
        "user_assignment_overrides",
        "user_settings_documents",
        "user_bulletin_subscriptions",
        "user_bulletin_states",
        "user_course_skipped_dates",
        "auth_sessions",
        "device_push_tokens",
        "push_jobs",
        "sync_jobs",
        "sync_log_entries",
        "user_sync_state",
    ]:
        try:
            # A savepoint: in a deregister's transaction a failed count would end it.
            async with conn.transaction():
                c = await conn.fetchval(
                    f"SELECT count(*) FROM {table} WHERE user_id = $1", user_id
                )
            counts[table] = c
        except Exception:
            counts[table] = "table_not_found"

    # Every device row, deregistered ones too: deleting the user cascades to all of them.
    counts["device_push_tokens"] = await conn.fetchval(
        "SELECT count(*) FROM device_push_tokens t"
        " JOIN user_devices d ON d.id = t.device_id WHERE d.user_id = $1",
        user_id,
    )

    ext_accounts = await conn.fetchval(
        "SELECT count(*) FROM external_accounts WHERE user_id = $1", user_id
    )
    ext_creds = await conn.fetchval(
        """SELECT count(*) FROM external_account_credentials
           WHERE external_account_id IN (
               SELECT id FROM external_accounts WHERE user_id = $1
           )""",
        user_id,
    )
    counts["external_accounts"] = ext_accounts
    counts["external_account_credentials"] = ext_creds
    return device_list, counts
