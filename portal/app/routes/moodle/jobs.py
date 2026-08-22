"""The sync-job queue: listing it, and the two retry-all paths."""

from __future__ import annotations
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from ...db import get_pool

router = APIRouter(prefix="/api/moodle")


class RetryAllRequest(BaseModel):
    notify: bool = False
@router.post("/retry-all")
async def retry_all_moodle(req: RetryAllRequest = RetryAllRequest(), pool=Depends(get_pool)):
    async with pool.acquire() as conn:
        result = await conn.execute("""
            UPDATE sync_jobs
            SET status = 'pending',
                run_after = now(),
                attempts = 0,
                locked_by = NULL,
                locked_at = NULL,
                last_error = NULL
            WHERE job_type IN ('moodle_assignments', 'ntust_courses')
              AND status IN ('disabled', 'failed')
        """)
        count = int(result.split()[-1]) if result else 0

        notified = 0
        if req.notify and count > 0:
            notified_result = await conn.execute("""
                INSERT INTO push_jobs (user_id, dedupe_key, channel, scenario, fire_at, payload)
                SELECT DISTINCT sj.user_id,
                       'system:moodle_expired:' || sj.user_id::text,
                       'system',
                       'reauth_required',
                       now(),
                       '{"reason": "operator_retry", "provider": "ntust_sso"}'::jsonb
                FROM sync_jobs sj
                WHERE sj.job_type IN ('moodle_assignments', 'ntust_courses')
                  AND sj.status = 'pending'
                  AND sj.attempts = 0
                ON CONFLICT DO NOTHING
            """)
            notified = int(notified_result.split()[-1]) if notified_result else 0

    return {"retried": count, "notified": notified}
@router.post("/retry-all-jobs")
async def retry_all_jobs(req: RetryAllRequest = RetryAllRequest(), pool=Depends(get_pool)):
    async with pool.acquire() as conn:
        result = await conn.execute("""
            UPDATE sync_jobs
            SET status = 'pending',
                run_after = now(),
                attempts = 0,
                locked_by = NULL,
                locked_at = NULL,
                last_error = NULL
            WHERE job_type IN ('moodle_assignments', 'ntust_courses')
              AND status IN ('active', 'disabled', 'failed')
        """)
        count = int(result.split()[-1]) if result else 0

        notified = 0
        if req.notify and count > 0:
            notified_result = await conn.execute("""
                INSERT INTO push_jobs (user_id, dedupe_key, channel, scenario, fire_at, payload)
                SELECT DISTINCT sj.user_id,
                       'system:moodle_expired:' || sj.user_id::text,
                       'system',
                       'reauth_required',
                       now(),
                       '{"reason": "operator_retry", "provider": "ntust_sso"}'::jsonb
                FROM sync_jobs sj
                WHERE sj.job_type IN ('moodle_assignments', 'ntust_courses')
                  AND sj.status = 'pending'
                  AND sj.attempts = 0
                ON CONFLICT DO NOTHING
            """)
            notified = int(notified_result.split()[-1]) if notified_result else 0

    return {"retried": count, "notified": notified}
@router.get("/jobs")
async def list_moodle_jobs(
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
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

    return [dict(r) for r in rows]
