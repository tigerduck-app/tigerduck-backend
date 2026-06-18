"""Moodle sync management tab — status, suspend, retry-all, job list."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..db import get_pool

router = APIRouter(prefix="/api/moodle")


class SuspendRequest(BaseModel):
    hours: float


@router.get("/status")
async def moodle_status(pool=Depends(get_pool)):
    async with pool.acquire() as conn:
        suspended = await conn.fetchrow(
            "SELECT value FROM system_settings WHERE key = 'moodle_suspended_until'"
        )
        suspended_until = None
        if suspended:
            until = (suspended["value"] or {}).get("until")
            if until:
                try:
                    dt = datetime.fromisoformat(until)
                    if dt > datetime.now(UTC):
                        suspended_until = until
                except ValueError:
                    pass

        counts = await conn.fetchrow("""
            SELECT
                COUNT(*) FILTER (WHERE status = 'pending')  AS active,
                COUNT(*) FILTER (WHERE status = 'running')  AS running,
                COUNT(*) FILTER (WHERE status = 'failed')   AS failed,
                COUNT(*) FILTER (WHERE status = 'disabled') AS disabled
            FROM sync_jobs
            WHERE job_type IN ('moodle_assignments', 'ntust_courses')
        """)

    return {
        "suspended_until": suspended_until,
        "job_counts": dict(counts) if counts else {
            "active": 0, "running": 0, "failed": 0, "disabled": 0
        },
    }


@router.post("/suspend")
async def suspend_moodle(req: SuspendRequest, pool=Depends(get_pool)):
    until = datetime.now(UTC) + timedelta(hours=req.hours)
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO system_settings (key, value, updated_at)
            VALUES ('moodle_suspended_until', $1::jsonb, now())
            ON CONFLICT (key) DO UPDATE SET value = $1::jsonb, updated_at = now()
        """, f'{{"until": "{until.isoformat()}"}}')
    return {"suspended_until": until.isoformat()}


@router.post("/resume")
async def resume_moodle(pool=Depends(get_pool)):
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM system_settings WHERE key = 'moodle_suspended_until'"
        )
    return {"resumed": True}


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


@router.get("/students")
async def list_moodle_students(
    credential_filter: str = Query(default="all"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    pool=Depends(get_pool),
):
    async with pool.acquire() as conn:
        counts = await conn.fetchrow("""
            SELECT
                COUNT(*) FILTER (WHERE ea.credential_status = 'active')  AS valid,
                COUNT(*) FILTER (WHERE ea.credential_status != 'active') AS expired
            FROM external_accounts ea
            WHERE ea.provider = 'ntust_sso'
        """)

        where = "WHERE ea.provider = 'ntust_sso'"
        params: list = []
        if credential_filter == "valid":
            where += " AND ea.credential_status = 'active'"
        elif credential_filter == "expired":
            where += " AND ea.credential_status != 'active'"

        rows = await conn.fetch(f"""
            SELECT
                u.student_id,
                ea.credential_status,
                ea.last_auth_success_at,
                ea.last_auth_failure_at,
                ea.last_auth_error,
                ea.updated_at
            FROM external_accounts ea
            JOIN users u ON u.id = ea.user_id
            {where}
            ORDER BY ea.updated_at DESC
            LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
        """, *params, limit, offset)

    return {
        "counts": dict(counts) if counts else {"valid": 0, "expired": 0},
        "students": [dict(r) for r in rows],
    }
