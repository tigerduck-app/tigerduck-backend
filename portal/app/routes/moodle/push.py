"""Manual push-pipeline controls: force a tick, clear the queue, or
trigger a client sync."""

from __future__ import annotations
import time
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from ...db import get_pool
from ._shared import logger

router = APIRouter(prefix="/api/moodle")


@router.post("/push-tick")
async def force_push_tick(request: Request) -> JSONResponse:
    """Proxy to the main API server's /push-tick endpoint."""
    import httpx
    from ..status import BACKEND_INTERNAL_URL
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{BACKEND_INTERNAL_URL}/push-tick")
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as e:
        logger.exception("force push tick proxy failed")
        return JSONResponse({"ok": False, "error": str(e)}, 500)
@router.post("/push-clear")
async def clear_push_queue(
    student_id: str = Query(..., min_length=1),
    pool=Depends(get_pool),
) -> JSONResponse:
    """Cancel all pending/processing push jobs for a user."""
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT id FROM users WHERE student_id = $1", student_id
        )
        if not user:
            return JSONResponse({"ok": False, "error": "user not found"}, 404)
        result = await conn.execute(
            "UPDATE push_jobs SET status = 'cancelled', cancelled_at = now() "
            "WHERE user_id = $1 AND status IN ('pending', 'processing')",
            user["id"],
        )
        count = int(result.split()[-1])
    return JSONResponse({"ok": True, "cancelled": count})
@router.post("/push-sync-trigger")
async def force_sync_trigger(
    student_id: str = Query(..., min_length=1),
    pool=Depends(get_pool),
    request: Request = None,
) -> JSONResponse:
    """Create a fresh sync_trigger push job and execute immediately."""
    import httpx
    from ..status import BACKEND_INTERNAL_URL

    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT id FROM users WHERE student_id = $1", student_id
        )
        if not user:
            return JSONResponse({"ok": False, "error": "user not found"}, 404)
        uid = user["id"]
        ts = int(time.time())
        row = await conn.fetchrow(
            "INSERT INTO push_jobs (user_id, dedupe_key, channel, scenario, fire_at, payload) "
            "VALUES ($1, $2, 'system', 'sync_trigger', now(), $3::jsonb) "
            "ON CONFLICT DO NOTHING RETURNING id",
            uid,
            f"sync_trigger:{uid}:{ts}",
            '{"kind":"sync_trigger","source":"portal_force"}',
        )
        job_id = row["id"] if row else None

    if job_id:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                await client.post(f"{BACKEND_INTERNAL_URL}/push-tick")
        except Exception:
            pass

    return JSONResponse({
        "ok": True,
        "push_job_id": job_id,
        "deduplicated": job_id is None,
    })
