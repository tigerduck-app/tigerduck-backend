"""Deregister (hard-delete) a student and all associated data.

Looks up the user by `student_id`, then DELETEs:
  - external_account_credentials (references external_accounts)
  - external_accounts             (references users)
  - users row                     (CASCADE wipes user_devices, auth_sessions,
                                   device_push_tokens, sync_jobs, push_jobs,
                                   bulletin_states, course overrides, etc.)

This is a destructive, irreversible operation.
"""
from __future__ import annotations

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
        # Look up the user by student_id.
        row = await conn.fetchrow(
            "SELECT id FROM users WHERE student_id = $1", student_id
        )
        if row is None:
            return JSONResponse(
                content={"error": "user_not_found"}, status_code=404
            )
        user_id = row["id"]

        # Delete credentials and external accounts that reference this
        # user (these tables don't cascade from users automatically).
        await conn.execute(
            """
            DELETE FROM external_account_credentials
            WHERE external_account_id IN (
                SELECT id FROM external_accounts WHERE user_id = $1
            )
            """,
            user_id,
        )
        await conn.execute(
            "DELETE FROM external_accounts WHERE user_id = $1", user_id
        )

        # Delete the user row — child tables with ON DELETE CASCADE
        # (user_devices, auth_sessions, device_push_tokens, sync state,
        # push_jobs, bulletin_states, etc.) are wiped automatically.
        await conn.execute("DELETE FROM users WHERE id = $1", user_id)

    return JSONResponse(
        content={
            "deleted": True,
            "student_id": student_id,
            "user_id": str(user_id),
        }
    )
