"""The sync-job queue: listing it, and the two retry-all paths."""

from __future__ import annotations
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from ...audit import audit
from ...db import get_pool

router = APIRouter(prefix="/api/moodle")


class RetryAllRequest(BaseModel):
    notify: bool = False


# The jobs of users waiting for a sign-in (a disabled job): only they get the
# expire notification (tigerduck-backend#54), and only with an iPhone or iPad
# syncing, the devices `mark_credentials_invalid` sends it to.
WAITING_TO_SIGN_IN = """
    FROM sync_jobs sj
    WHERE job_type IN ('moodle_assignments', 'ntust_courses')
      AND status = 'disabled'
      AND EXISTS (
          SELECT 1 FROM user_devices d
          WHERE d.user_id = sj.user_id AND d.deleted_at IS NULL
            AND d.cloud_sync_enabled AND d.platform IN ('ios', 'ipados')
      )
"""


async def _waiting_to_sign_in(conn) -> list:
    """Those users, locked for the reset that follows."""
    rows = await conn.fetch(f"SELECT user_id {WAITING_TO_SIGN_IN} FOR UPDATE OF sj")
    return list({r["user_id"] for r in rows})


async def _notify_expired(conn, user_ids: list) -> int:
    if not user_ids:
        return 0
    # Keyed by when the failing credential was stored, as the server keys its
    # own prompt: a sent one keeps its key for days, and the next must go.
    result = await conn.execute("""
        INSERT INTO push_jobs (user_id, dedupe_key, channel, scenario, fire_at, payload)
        SELECT u,
               'system:moodle_expired:' || u::text || coalesce(
                   ':' || (extract(epoch FROM ea.last_auth_success_at) * 1000000)::bigint,
                   ''
               ),
               'system',
               'reauth_required',
               now(),
               '{"reason": "operator_retry", "provider": "ntust_sso"}'::jsonb
        FROM unnest($1::uuid[]) AS u
        LEFT JOIN external_accounts ea ON ea.user_id = u AND ea.provider = 'ntust_sso'
        ON CONFLICT DO NOTHING
    """, user_ids)
    return int(result.split()[-1]) if result else 0
@router.post("/retry-all")
async def retry_all_moodle(
    request: Request, req: RetryAllRequest = RetryAllRequest(), pool=Depends(get_pool)
):
    # A job whose last run failed on the school's side stays pending until
    # its policy interval (server/syncjobs/executor.py), and keeps its
    # last_error until a run succeeds.
    async with pool.acquire() as conn, conn.transaction():
        waiting = await _waiting_to_sign_in(conn) if req.notify else []
        result = await conn.execute("""
            UPDATE sync_jobs
            SET status = 'pending',
                run_after = now(),
                attempts = 0,
                locked_by = NULL,
                locked_at = NULL,
                last_error = NULL
            WHERE job_type IN ('moodle_assignments', 'ntust_courses')
              AND (status IN ('disabled', 'failed')
                   OR (status = 'pending' AND last_error IS NOT NULL))
        """)
        count = int(result.split()[-1]) if result else 0
        notified = await _notify_expired(conn, waiting)
        await audit(
            conn, request, "sync.retry_failed",
            notify=req.notify, retried=count, notified=notified,
        )

    return {"retried": count, "notified": notified}
@router.post("/retry-all-jobs")
async def retry_all_jobs(
    request: Request, req: RetryAllRequest = RetryAllRequest(), pool=Depends(get_pool)
):
    async with pool.acquire() as conn, conn.transaction():
        waiting = await _waiting_to_sign_in(conn) if req.notify else []
        result = await conn.execute("""
            UPDATE sync_jobs
            SET status = 'pending',
                run_after = now(),
                attempts = 0,
                locked_by = NULL,
                locked_at = NULL,
                last_error = NULL
            WHERE job_type IN ('moodle_assignments', 'ntust_courses')
              AND status IN ('pending', 'disabled', 'failed')
        """)
        count = int(result.split()[-1]) if result else 0
        notified = await _notify_expired(conn, waiting)
        await audit(
            conn, request, "sync.retry_all",
            notify=req.notify, retried=count, notified=notified,
        )

    return {"retried": count, "notified": notified}
@router.get("/jobs")
async def list_moodle_jobs(
    request: Request,
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    pool=Depends(get_pool),
):
    where = "WHERE sj.job_type IN ('moodle_assignments', 'ntust_courses')"
    params: list = []
    if status:
        params.append(status)
        where += f" AND sj.status = ${len(params)}"

    async with pool.acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT
                sj.id,
                u.student_id,
                sj.job_type,
                sj.status,
                sj.attempts,
                sj.last_success_at,
                sj.last_failure_at,
                sj.last_error,
                sj.run_after,
                sj.created_at
            FROM sync_jobs sj
            JOIN users u ON u.id = sj.user_id
            {where}
            ORDER BY sj.updated_at DESC
            LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
        """, *params, limit, offset)
        await audit(conn, request, "user.list", count=len(rows))

    return {"jobs": [dict(r) for r in rows]}
