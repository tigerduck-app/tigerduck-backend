"""Manual push-pipeline controls: force a tick, clear the queue, or
trigger a client sync."""

from __future__ import annotations
import time
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from ...db import get_pool
from ._shared import logger

router = APIRouter(prefix="/api/moodle")


def _internal_headers(request: Request | None) -> dict[str, str]:
    """Auth header for the backend's internal endpoints.

    `/push-tick` is behind `require_shared_secret`, which reads
    `X-Push-Token`. Omitted when no secret is configured so a dev stack —
    where the dependency short-circuits on an empty secret — keeps working
    without one.
    """
    # `force_sync_trigger` declares `request: Request = None`; FastAPI always
    # injects it, but the annotation permits None so this stays total.
    if request is None:
        return {}
    settings = getattr(request.app.state, "settings", None)
    token = getattr(settings, "api_shared_secret", "") if settings else ""
    return {"X-Push-Token": token} if token else {}


@router.post("/push-tick")
async def force_push_tick(request: Request) -> JSONResponse:
    """Proxy to the main API server's /push-tick endpoint.

    Carries the shared secret: `/push-tick` sits behind
    `require_shared_secret`, so an unauthenticated proxy call is refused
    the moment `TIGERDUCK_API_SHARED_SECRET` is set — which it always is
    outside dev, where the dependency short-circuits on an empty secret.
    That is why this worked locally and not in a real deployment.
    """
    import httpx
    from ..status import BACKEND_INTERNAL_URL
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{BACKEND_INTERNAL_URL}/push-tick",
                headers=_internal_headers(request),
            )
            # A non-JSON body (an HTML 502 from a proxy, say) must not
            # become a bare exception with no reason attached — carry the
            # status and whatever text came back instead.
            try:
                payload = resp.json()
            except ValueError:
                payload = {
                    "ok": False,
                    "error": f"HTTP {resp.status_code}: {resp.text[:300]}",
                }
            return JSONResponse(payload, status_code=resp.status_code)
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
                tick = await client.post(
                    f"{BACKEND_INTERNAL_URL}/push-tick",
                    headers=_internal_headers(request),
                )
            # Logged rather than swallowed: the job row is already written,
            # so a refused tick means the push sits queued until the next
            # scheduled run — which looks to an operator like the trigger
            # silently did nothing.
            if tick.status_code >= 400:
                logger.warning(
                    "sync-trigger follow-up tick refused: HTTP %s %s",
                    tick.status_code,
                    tick.text[:200],
                )
        except Exception:
            logger.exception("sync-trigger follow-up tick failed")

    return JSONResponse({
        "ok": True,
        "push_job_id": job_id,
        "deduplicated": job_id is None,
    })
