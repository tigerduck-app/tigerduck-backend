"""The audit log (`app/audit.py` writes it), newest first, as JSON."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from ..audit import audit
from ..db import get_pool

router = APIRouter(prefix="/api/audit")

_LIST_SQL = """
SELECT a.id, a.ts, a.actor, a.action, a.target_user_id, a.target_device_id,
       a.detail, u.student_id
FROM admin_audit_log a
LEFT JOIN users u ON u.id = a.target_user_id
ORDER BY a.ts DESC, a.id DESC
LIMIT $1 OFFSET $2
"""


def _str_or_none(value) -> str | None:
    return None if value is None else str(value)


@router.get("")
async def list_audit(
    request: Request,
    pool=Depends(get_pool),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> JSONResponse:
    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT count(*) FROM admin_audit_log")
        rows = await conn.fetch(_LIST_SQL, limit, offset)
        await audit(conn, request, "user.list", count=len(rows))
    return JSONResponse(
        {
            "items": [
                {
                    "id": r["id"],
                    "ts": r["ts"].isoformat(),
                    "actor": r["actor"],
                    "action": r["action"],
                    "target_user_id": _str_or_none(r["target_user_id"]),
                    # Null once the account is deleted; the UUID stays.
                    "student_id": r["student_id"],
                    "target_device_id": _str_or_none(r["target_device_id"]),
                    "detail": json.loads(r["detail"]),
                }
                for r in rows
            ],
            "total": int(total),
        },
        headers={"Cache-Control": "private, no-store"},
    )
