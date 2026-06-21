"""Moodle sync management tab — status, suspend, retry-all, job list, sync logs."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..db import get_pool

logger = logging.getLogger(__name__)
_log_table_ready = False

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


@router.get("/sync-events")
async def sync_events(
    student_id: str = Query(..., min_length=1),
    limit: int = Query(default=100, ge=1, le=500),
    pool=Depends(get_pool),
):
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT id FROM users WHERE student_id = $1", student_id
        )
        if not user:
            return {"student_id": student_id, "found": False, "events": []}

        uid = user["id"]

        jobs = await conn.fetch(
            "SELECT id, job_type, status AS job_status, attempts, "
            "last_success_at, last_failure_at, last_error, run_after "
            "FROM sync_jobs WHERE user_id = $1 "
            "ORDER BY job_type",
            uid,
        )

        runs = await conn.fetch(
            "SELECT sr.id, sj.job_type, sr.started_at, sr.finished_at, "
            "sr.status, sr.fetched_count, sr.changed_count, sr.error, "
            "sr.metadata AS meta "
            "FROM sync_runs sr "
            "JOIN sync_jobs sj ON sj.id = sr.sync_job_id "
            "WHERE sr.user_id = $1 "
            "ORDER BY sr.started_at DESC LIMIT $2",
            uid, limit,
        )

        overrides = await conn.fetch(
            "SELECT ua.moodle_assignment_id, ua.title, "
            "o.local_status, o.updated_at "
            "FROM user_assignment_overrides o "
            "JOIN user_assignments ua ON ua.id = o.user_assignment_id "
            "WHERE o.user_id = $1 "
            "ORDER BY o.updated_at DESC LIMIT 50",
            uid,
        )

        devices = await conn.fetch(
            "SELECT id, client_device_id, platform, "
            "app_version, os_version, last_seen_at, last_login_at, created_at, "
            "sync_courses, sync_course_colors, sync_course_names, sync_assignments "
            "FROM user_devices WHERE user_id = $1 "
            "ORDER BY last_seen_at DESC NULLS LAST",
            uid,
        )

        push_jobs = await conn.fetch(
            "SELECT pj.id, pj.scenario, pj.status, pj.attempts, pj.max_attempts, "
            "pj.fire_at, pj.sent_at, pj.last_error, pj.dedupe_key, pj.created_at, "
            "pj.payload->>'source_device_id' AS source_device_id "
            "FROM push_jobs pj "
            "WHERE pj.user_id = $1 AND pj.status IN ('pending', 'processing') "
            "ORDER BY pj.created_at DESC LIMIT 20",
            uid,
        )

        push_deliveries = []
        if push_jobs:
            pj_ids = [pj["id"] for pj in push_jobs]
            push_deliveries = await conn.fetch(
                "SELECT pd.id, pd.push_job_id, pd.device_id, pd.provider, "
                "pd.status, pd.attempts, pd.max_attempts, pd.failure_code, "
                "pd.failure_message, pd.sent_at, pd.created_at "
                "FROM push_deliveries pd "
                "WHERE pd.push_job_id = ANY($1::bigint[]) "
                "ORDER BY pd.created_at",
                pj_ids,
            )

        topology = await conn.fetchrow(
            "SELECT "
            "  (SELECT current_revision FROM user_sync_state WHERE user_id = $1) AS revision, "
            "  (SELECT count(*) FROM user_courses WHERE user_id = $1) AS course_count, "
            "  (SELECT count(*) FROM user_course_tombstones WHERE user_id = $1) AS tombstone_count, "
            "  (SELECT courses_reset_at FROM users WHERE id = $1) AS courses_reset_at",
            uid,
        )

    poll_status = {}
    try:
        from server.sync.poll_tracker import is_foreground
        for d in devices:
            cid = d["client_device_id"]
            poll_status[str(d["id"])] = "foreground" if is_foreground(str(uid), cid) else "background"
    except ImportError:
        pass

    return {
        "student_id": student_id,
        "found": True,
        "topology": {
            "revision": topology["revision"] or 0 if topology else 0,
            "course_count": topology["course_count"] or 0 if topology else 0,
            "tombstone_count": topology["tombstone_count"] or 0 if topology else 0,
            "courses_reset_at": str(topology["courses_reset_at"]) if topology and topology["courses_reset_at"] else None,
        },
        "poll_status": poll_status,
        "jobs": [dict(j) for j in jobs],
        "runs": [dict(r) for r in runs],
        "overrides": [dict(o) for o in overrides],
        "devices": [dict(d) for d in devices],
        "push_jobs": [dict(p) for p in push_jobs],
        "push_deliveries": [dict(d) for d in push_deliveries],
    }


COURSE_PALETTE_LIGHT = [
    "#FF6B6B", "#4ECDC4", "#45B7D1", "#F39C12", "#DDA0DD",
    "#2ECC71", "#E74C3C", "#3498DB", "#F7DC6F", "#9B59B6",
    "#1ABC9C", "#E67E22", "#85C1E9", "#D35400", "#27AE60",
    "#C0392B", "#8E44AD", "#16A085", "#F1C40F", "#2980B9",
]
COURSE_PALETTE_DARK = [
    "#994747", "#3A7A73", "#366D7D", "#916111", "#846284",
    "#267A4C", "#8A3329", "#265F83", "#948448", "#5E3E6E",
    "#13715E", "#8A5118", "#54748C", "#7E3700", "#1E693D",
    "#73281D", "#562D68", "#126150", "#91750F", "#1E4F6F",
]


def _course_hash_index(course_no: str) -> int:
    h = 0
    for c in course_no:
        h = (h * 31 + ord(c)) & 0x7FFFFFFF
    return h % len(COURSE_PALETTE_LIGHT)


_cn_cache: dict[str, str] = {}
_cn_cache_ts: dict[str, float] = {}
_CN_CACHE_TTL = 3600


def _fetch_single_course_name_sync(
    semester: str, lang: str, course_no: str,
) -> str | None:
    """Blocking fetch of one course name — meant to run in a thread."""
    import ssl
    import urllib.request
    import json as _json

    try:
        payload = _json.dumps({
            "Semester": semester,
            "CourseNo": course_no, "CourseName": "", "CourseTeacher": "",
            "Dimension": "", "CourseNotes": "", "CampusNotes": "",
            "ForeignLanguage": 0, "OnlyIntensive": 0, "OnlyGeneral": 0,
            "OnleyNTUST": 0, "OnlyMaster": 0, "OnlyUnderGraduate": 0,
            "OnlyNode": 0, "Language": lang,
        }).encode()
        req = urllib.request.Request(
            "https://querycourse.ntust.edu.tw/QueryCourse/api/courses",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            courses = _json.loads(resp.read())
        for c in courses:
            if c.get("CourseNo") == course_no:
                return c.get("CourseName")
    except Exception as e:
        logger.warning("course-name lookup failed %s/%s/%s: %s", semester, lang, course_no, e)
    return None


async def _course_names_for_nos(
    semester: str, lang: str, course_nos: list[str],
) -> dict[str, str]:
    """Fetch course names for specific course numbers, with per-entry cache."""
    now = time.monotonic()
    result: dict[str, str] = {}
    to_fetch: list[str] = []

    for no in course_nos:
        key = f"{semester}:{lang}:{no}"
        if key in _cn_cache and (now - _cn_cache_ts.get(key, 0)) < _CN_CACHE_TTL:
            result[no] = _cn_cache[key]
        else:
            to_fetch.append(no)

    if to_fetch:
        loop = asyncio.get_event_loop()
        names = await asyncio.gather(*(
            loop.run_in_executor(None, _fetch_single_course_name_sync, semester, lang, no)
            for no in to_fetch
        ))
        for no, name in zip(to_fetch, names):
            key = f"{semester}:{lang}:{no}"
            if name:
                _cn_cache[key] = name
                _cn_cache_ts[key] = now
                result[no] = name

    return result


def _current_semester_prefix() -> str:
    """NTUST semester prefix: e.g. '1132' for 2024 spring semester."""
    now = datetime.now(UTC)
    roc_year = now.year - 1911
    if now.month >= 8:
        return f"{roc_year}1"
    elif now.month >= 2:
        return f"{roc_year - 1}2"
    else:
        return f"{roc_year - 1}1"


@router.get("/sync-courses")
async def sync_courses(
    student_id: str = Query(..., min_length=1),
    pool=Depends(get_pool),
):
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT id FROM users WHERE student_id = $1", student_id
        )
        if not user:
            return {"courses": []}

        uid = user["id"]
        prefix = _current_semester_prefix()

        rows = await conn.fetch(
            "SELECT c.id, c.moodle_id, c.course_no, c.course_name, "
            "c.course_name_en, c.source, c.semester, "
            "c.updated_by_device_id, c.updated_at, "
            "o.color_hex, o.custom_names, "
            "o.color_hex_device_id "
            "FROM user_courses c "
            "LEFT JOIN user_course_overrides o "
            "  ON o.user_id = c.user_id AND o.user_course_id = c.id "
            "WHERE c.user_id = $1 AND c.semester = $2 "
            "ORDER BY c.course_name",
            uid, prefix,
        )

        tombstones = await conn.fetch(
            "SELECT course_key, course_no, semester, deleted_at, "
            "deleted_by_device_id "
            "FROM user_course_tombstones WHERE user_id = $1 "
            "ORDER BY deleted_at DESC",
            uid,
        )

        assignments = await conn.fetch(
            "SELECT a.id, a.moodle_assignment_id, a.course_no, "
            "a.course_name, a.title, a.due_at, a.moodle_url, "
            "a.provider_is_submitted, a.provider_grade "
            "FROM user_assignments a "
            "WHERE a.user_id = $1 AND a.deleted_at IS NULL "
            "ORDER BY a.due_at DESC NULLS LAST LIMIT 50",
            uid,
        )

    course_nos = list({r["course_no"] for r in rows if r["course_no"]})
    zh_names, en_names = await asyncio.gather(
        _course_names_for_nos(prefix, "zh", course_nos),
        _course_names_for_nos(prefix, "en", course_nos),
    )

    courses = []
    for r in rows:
        d = dict(r)
        course_no = d.get("course_no") or ""
        zh_name = zh_names.get(course_no)
        if zh_name:
            d["course_name"] = zh_name
        else:
            name = d.get("course_name") or ""
            bracket_end = name.find("】")
            if bracket_end >= 0:
                rest = name[bracket_end + 1:].strip()
                d["course_name"] = rest.split(" ", 1)[-1] if " " in rest else rest
        en_name = en_names.get(course_no)
        if en_name:
            d["course_name_en"] = en_name
        d["client_course_no"] = course_no
        idx = _course_hash_index(course_no)
        d["default_palette_index"] = idx
        d["default_color_light"] = COURSE_PALETTE_LIGHT[idx]
        d["default_color_dark"] = COURSE_PALETTE_DARK[idx]
        courses.append(d)

    return {
        "semester": prefix,
        "palette_light": COURSE_PALETTE_LIGHT,
        "palette_dark": COURSE_PALETTE_DARK,
        "courses": courses,
        "tombstones": [dict(t) for t in tombstones],
        "assignments": [dict(a) for a in assignments],
    }


@router.get("/sync-logs")
async def sync_logs(
    student_id: str = Query(..., min_length=1),
    after_id: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
    pool=Depends(get_pool),
):
    global _log_table_ready
    async with pool.acquire() as conn:
        if not _log_table_ready:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS sync_log_entries (
                    id          BIGSERIAL PRIMARY KEY,
                    user_id     UUID NOT NULL,
                    device_id   UUID,
                    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
                    level       VARCHAR(8) NOT NULL DEFAULT 'INFO',
                    source      VARCHAR(32) NOT NULL,
                    message     TEXT NOT NULL,
                    detail      JSONB
                )
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_sync_log_entries_user
                    ON sync_log_entries (user_id, ts DESC)
            """)
            await conn.execute(
                "ALTER TABLE sync_log_entries ADD COLUMN IF NOT EXISTS device_id UUID"
            )
            _log_table_ready = True

        user = await conn.fetchrow(
            "SELECT id FROM users WHERE student_id = $1", student_id
        )
        if not user:
            return {"entries": [], "latest_id": after_id}

        rows = await conn.fetch(
            "SELECT e.id, e.ts, e.level, e.source, e.message, e.detail, "
            "d.client_device_id AS device_label, d.platform "
            "FROM sync_log_entries e "
            "LEFT JOIN user_devices d ON d.id = e.device_id "
            "WHERE e.user_id = $1 AND e.id > $2 "
            "ORDER BY e.id ASC LIMIT $3",
            user["id"], after_id, limit,
        )

    entries = [dict(r) for r in rows]
    latest_id = entries[-1]["id"] if entries else after_id
    return {"entries": entries, "latest_id": latest_id}


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
