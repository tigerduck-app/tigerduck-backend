"""Read-only views of one student's synced data, for support work.

Course names are fetched one at a time from NTUST and memoised in a
process-local TTL cache — the admin UI asks for the same handful of
course numbers repeatedly while someone clicks around."""

from __future__ import annotations
import asyncio
import time
from datetime import UTC, datetime, timedelta
from fastapi import APIRouter, Depends, Query, Request
from ...db import get_pool
from ._shared import logger

router = APIRouter(prefix="/api/moodle")


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
            "sync_courses, sync_course_colors, sync_course_names, sync_assignments, "
            "cloud_sync_enabled "
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
