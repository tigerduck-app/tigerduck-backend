"""Scheduler status and the suspend/resume switch."""

from __future__ import annotations
from datetime import UTC, datetime, timedelta
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from ...db import get_pool

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
@router.get("/stats")
async def moodle_stats(pool=Depends(get_pool)):
    async with pool.acquire() as conn:
        summary = await conn.fetchrow("""
            SELECT
                COUNT(*) FILTER (WHERE status = 'succeeded') AS succeeded_24h,
                COUNT(*) FILTER (WHERE status = 'failed')    AS failed_24h,
                COUNT(*) FILTER (WHERE status = 'running')   AS running_now,
                COUNT(*)                                     AS total_24h,
                AVG(EXTRACT(EPOCH FROM (finished_at - started_at)))
                    FILTER (WHERE finished_at IS NOT NULL)   AS avg_duration_s,
                SUM(fetched_count)
                    FILTER (WHERE status = 'succeeded')      AS total_fetched
            FROM sync_runs
            WHERE started_at > now() - INTERVAL '24 hours'
        """)
        recent = await conn.fetch("""
            SELECT
                sr.id, sj.job_type, sr.status, sr.started_at, sr.finished_at,
                sr.fetched_count, sr.changed_count,
                u.student_id
            FROM sync_runs sr
            JOIN sync_jobs sj ON sj.id = sr.sync_job_id
            JOIN users u ON u.id = sj.user_id
            ORDER BY sr.started_at DESC
            LIMIT 50
        """)
    return {
        "summary": {
            "succeeded_24h": summary["succeeded_24h"] or 0,
            "failed_24h": summary["failed_24h"] or 0,
            "running_now": summary["running_now"] or 0,
            "total_24h": summary["total_24h"] or 0,
            "avg_duration_s": round(float(summary["avg_duration_s"] or 0), 1),
            "total_fetched": summary["total_fetched"] or 0,
        },
        "recent_runs": [
            {
                "id": r["id"],
                "student_id": r["student_id"],
                "job_type": r["job_type"],
                "status": r["status"],
                "started_at": r["started_at"].isoformat() if r["started_at"] else None,
                "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None,
                "fetched_count": r["fetched_count"],
                "changed_count": r["changed_count"],
            }
            for r in recent
        ],
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
