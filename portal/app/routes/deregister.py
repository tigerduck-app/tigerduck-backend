"""Deregister (hard-delete) a student and all associated data.

Looks up the user by `student_id`, collects a deletion report
(counts + device IDs), then DELETEs everything. The report is
returned so the frontend can offer it as a downloadable file.
"""
from __future__ import annotations

from datetime import datetime, UTC

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..db import get_pool

router = APIRouter(prefix="/api/deregister")


class DeregisterRequest(BaseModel):
    student_id: str


@router.post("")
async def deregister_student(
    body: DeregisterRequest,
    pool=Depends(get_pool),
) -> JSONResponse:
    student_id = body.student_id.strip()
    if not student_id:
        return JSONResponse(
            content={"error": "student_id_required"}, status_code=400
        )

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, student_id, display_name, created_at FROM users WHERE student_id = $1",
            student_id,
        )
        if row is None:
            return JSONResponse(
                content={"error": "user_not_found"}, status_code=404
            )
        user_id = row["id"]

        devices = await conn.fetch(
            "SELECT id, client_device_id, platform, device_name, app_version, last_seen_at "
            "FROM user_devices WHERE user_id = $1",
            user_id,
        )
        device_list = [
            {
                "device_id": str(d["id"]),
                "client_device_id": d["client_device_id"],
                "platform": d["platform"],
                "device_name": d["device_name"],
                "app_version": d["app_version"],
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
                c = await conn.fetchval(
                    f"SELECT count(*) FROM {table} WHERE user_id = $1", user_id
                )
                counts[table] = c
            except Exception:
                counts[table] = "table_not_found"

        token_count = 0
        for d in devices:
            c = await conn.fetchval(
                "SELECT count(*) FROM device_push_tokens WHERE device_id = $1",
                d["id"],
            )
            token_count += c
        counts["device_push_tokens"] = token_count

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

        await conn.execute(
            """DELETE FROM external_account_credentials
               WHERE external_account_id IN (
                   SELECT id FROM external_accounts WHERE user_id = $1
               )""",
            user_id,
        )
        await conn.execute(
            "DELETE FROM external_accounts WHERE user_id = $1", user_id
        )
        await conn.execute("DELETE FROM users WHERE id = $1", user_id)

    report = {
        "deleted": True,
        "timestamp": datetime.now(UTC).isoformat(),
        "student_id": student_id,
        "user_id": str(user_id),
        "display_name": row["display_name"],
        "account_created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "devices": device_list,
        "deleted_counts": counts,
    }
    return JSONResponse(content=report)
