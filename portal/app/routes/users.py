"""Users: the Status page's numbers, its user list, and one user's page.

Read from the v3 tables, as `devices.py` does. Days and weeks are Taipei
ones, the school's. Daily activity is a user's session renewals: the apps
renew their 15-minute access token while open, and every renewal adds an
`auth_sessions` row that nothing prunes.
"""
from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request

from ..audit import audit
from ..db import get_pool
from .moodle._push_queue import payload_of, summary

router = APIRouter(prefix="/api/users")

_TODAY = "(now() AT TIME ZONE 'Asia/Taipei')::date"

# For an NTUST account `ea`: signed out, a sync job that gave up, or fine.
_ACCOUNT_STATE = """
CASE
    WHEN ea.id IS NULL THEN NULL
    WHEN ea.credential_status <> 'active' THEN 'needs_sign_in'
    WHEN EXISTS (
        SELECT 1 FROM sync_jobs j WHERE j.user_id = ea.user_id AND j.status = 'failed'
    ) THEN 'failing'
    ELSE 'signed_in'
END
"""

# server/push/course_reminders.py `_course_prefs`: on unless the user's
# notification document turns `courses.enabled` falsy.
_CLASS_REMINDERS_ON = """
COALESCE(
    n.document #> '{courses,enabled}' NOT IN ('false', 'null', '0', '""'), true
)
"""

_NOTIFICATION_DOC = """
LEFT JOIN user_settings_documents n
    ON n.user_id = u.id AND n.namespace = 'notification' AND n.deleted_at IS NULL
"""

_USERS_SQL = """
SELECT count(*) AS total,
       count(*) FILTER (WHERE created_at > now() - interval '7 days') AS new_7d
FROM users WHERE deleted_at IS NULL
"""

_WEEKLY_SQL = """
WITH joined AS (
    SELECT date_trunc('week', created_at AT TIME ZONE 'Asia/Taipei') AS week, count(*) AS users
    FROM users
    WHERE deleted_at IS NULL AND created_at > now() - interval '13 weeks'
    GROUP BY 1
)
SELECT w::date AS week, COALESCE(j.users, 0) AS users
FROM generate_series(
    date_trunc('week', now() AT TIME ZONE 'Asia/Taipei') - interval '11 weeks',
    date_trunc('week', now() AT TIME ZONE 'Asia/Taipei'),
    interval '1 week'
) AS w
LEFT JOIN joined j ON j.week = w
ORDER BY w
"""

_RENEWALS = """
FROM auth_sessions s
JOIN users u ON u.id = s.user_id AND u.deleted_at IS NULL
"""

_DAILY_ACTIVE_SQL = f"""
WITH active AS (
    SELECT (s.issued_at AT TIME ZONE 'Asia/Taipei')::date AS day,
           count(DISTINCT s.user_id) AS users
    {_RENEWALS}
    WHERE s.issued_at > now() - interval '91 days'
    GROUP BY 1
)
SELECT d::date AS day, COALESCE(a.users, 0) AS users
FROM generate_series({_TODAY} - 89, {_TODAY}, interval '1 day') AS d
LEFT JOIN active a ON a.day = d::date
ORDER BY d
"""

_ACTIVE_7D_SQL = f"""
SELECT count(DISTINCT s.user_id) {_RENEWALS}
WHERE s.issued_at > now() - interval '7 days'
"""

_LIVE_DEVICES = """
FROM user_devices ud
JOIN users u ON u.id = ud.user_id AND u.deleted_at IS NULL
WHERE ud.deleted_at IS NULL
"""

_VERSIONS_SQL = f"""
SELECT ud.platform, COALESCE(ud.app_version, '') AS app_version, count(*) AS devices
{_LIVE_DEVICES}
GROUP BY 1, 2
ORDER BY 1, 2
"""

# Class reminders are a user's setting, so they count users with a device.
_FEATURES_SQL = f"""
SELECT
    count(DISTINCT u.id) AS users,
    count(DISTINCT u.id) FILTER (WHERE {_CLASS_REMINDERS_ON}) AS class_reminders,
    count(*) FILTER (WHERE ud.bulletin_push_enabled) AS bulletin_push,
    count(*) FILTER (WHERE ud.sync_live_activity) AS live_activity,
    count(*) FILTER (WHERE ud.sync_assignment_reminders) AS assignment_reminders,
    count(*) FILTER (WHERE ud.cloud_sync_enabled) AS cloud_sync,
    count(*) FILTER (WHERE ud.sync_course_names) AS course_name_sync
FROM user_devices ud
JOIN users u ON u.id = ud.user_id AND u.deleted_at IS NULL
{_NOTIFICATION_DOC}
WHERE ud.deleted_at IS NULL
"""

# Devices signed in at the end of each day, from their sign-in and removal stamps.
_DAILY_DEVICES_SQL = f"""
SELECT d::date AS day, (
    SELECT count(*) FROM user_devices ud JOIN users u ON u.id = ud.user_id
    WHERE ud.created_at < t.ends
      AND (ud.deleted_at IS NULL OR ud.deleted_at >= t.ends)
      AND (u.deleted_at IS NULL OR u.deleted_at >= t.ends)
) AS devices
FROM generate_series({_TODAY} - 19, {_TODAY}, interval '1 day') AS d,
     LATERAL (SELECT (d::date + 1)::timestamp AT TIME ZONE 'Asia/Taipei' AS ends) AS t
ORDER BY d
"""

_ANONYMOUS_SQL = "SELECT count(*) FROM device_registrations WHERE linked_user_id IS NULL"

_ACCOUNTS_SQL = f"""
SELECT {_ACCOUNT_STATE} AS state, count(*) AS users
FROM external_accounts ea
JOIN users u ON u.id = ea.user_id AND u.deleted_at IS NULL
WHERE ea.provider = 'ntust_sso'
GROUP BY 1
"""

# Push jobs are pruned after 7 days (server/push/retention.py). A job that found
# no device fails without a delivery; anonymous devices take bulletins without a job.
_OUTCOMES = """
WITH outcomes AS (
    SELECT j.channel, pd.status, COALESCE(pd.sent_at, pd.updated_at) AS at
    FROM push_deliveries pd JOIN push_jobs j ON j.id = pd.push_job_id
    WHERE pd.status IN ('sent', 'failed')
    UNION ALL
    SELECT j.channel, j.status, j.updated_at
    FROM push_jobs j
    WHERE j.status = 'failed' AND NOT EXISTS (
        SELECT 1 FROM push_deliveries pd
        WHERE pd.push_job_id = j.id AND pd.status IN ('sent', 'failed')
    )
    UNION ALL
    SELECT 'bulletin', status, COALESCE(sent_at, updated_at)
    FROM bulletin_dispatches
    WHERE status IN ('sent', 'failed') AND COALESCE(sent_at, updated_at) > now() - interval '8 days'
)
"""

_CHANNELS_SQL = f"""
{_OUTCOMES}
SELECT channel,
       count(*) FILTER (WHERE status = 'sent') AS sent,
       count(*) FILTER (WHERE status = 'failed') AS failed
FROM outcomes
WHERE at > now() - interval '24 hours'
GROUP BY channel
ORDER BY sent DESC, channel
"""

_DAILY_PUSHES_SQL = f"""
{_OUTCOMES}
SELECT d::date AS day,
       count(o.status) FILTER (WHERE o.status = 'sent') AS sent,
       count(o.status) FILTER (WHERE o.status = 'failed') AS failed
FROM generate_series({_TODAY} - 6, {_TODAY}, interval '1 day') AS d
LEFT JOIN outcomes o ON (o.at AT TIME ZONE 'Asia/Taipei')::date = d::date
GROUP BY d
ORDER BY d
"""


@router.get("/stats")
async def user_stats(pool=Depends(get_pool)) -> dict:
    """Counts only, so no audit row: no student is named."""
    async with pool.acquire() as conn:
        users = await conn.fetchrow(_USERS_SQL)
        weekly = await conn.fetch(_WEEKLY_SQL)
        daily_active = await conn.fetch(_DAILY_ACTIVE_SQL)
        active_7d = await conn.fetchval(_ACTIVE_7D_SQL)
        versions = await conn.fetch(_VERSIONS_SQL)
        features = dict(await conn.fetchrow(_FEATURES_SQL))
        daily_devices = await conn.fetch(_DAILY_DEVICES_SQL)
        anonymous = await conn.fetchval(_ANONYMOUS_SQL)
        accounts = {r["state"]: r["users"] for r in await conn.fetch(_ACCOUNTS_SQL)}
        channels = await conn.fetch(_CHANNELS_SQL)
        daily_pushes = await conn.fetch(_DAILY_PUSHES_SQL)
    return {
        "users": {**dict(users), "weekly": [dict(r) for r in weekly]},
        "active": {"last_7d": active_7d, "daily": [dict(r) for r in daily_active]},
        "devices": {
            "versions": [dict(r) for r in versions],
            "users": features.pop("users"),
            "features": features,
            "daily": [dict(r) for r in daily_devices],
        },
        "anonymous_devices": anonymous,
        "accounts": {k: accounts.get(k, 0) for k in ("signed_in", "needs_sign_in", "failing")},
        "pushes": {
            "channels": [dict(r) for r in channels],
            "daily": [dict(r) for r in daily_pushes],
        },
    }


# shortcut: the Status table filters in the browser, so the list is capped;
# page it on the server once users outgrow this.
_LIST_LIMIT = 5000

_LIST_SQL = f"""
SELECT u.id, u.student_id, u.display_name, u.created_at,
       COALESCE(d.platforms, '{{}}') AS platforms, d.last_active_at, d.cloud_sync,
       {_ACCOUNT_STATE} AS account
FROM users u
LEFT JOIN LATERAL (
    SELECT array_agg(platform ORDER BY created_at, id) AS platforms,
           max(last_seen_at) AS last_active_at,
           bool_or(cloud_sync_enabled) AS cloud_sync
    FROM user_devices WHERE user_id = u.id AND deleted_at IS NULL
) d ON true
LEFT JOIN external_accounts ea ON ea.user_id = u.id AND ea.provider = 'ntust_sso'
WHERE u.deleted_at IS NULL
ORDER BY d.last_active_at DESC NULLS LAST, u.created_at DESC
LIMIT $1
"""


@router.get("")
async def list_users(request: Request, pool=Depends(get_pool)) -> dict:
    async with pool.acquire() as conn:
        rows = await conn.fetch(_LIST_SQL, _LIST_LIMIT)
        await audit(conn, request, "user.list", count=len(rows))
    return {"items": [dict(r) for r in rows], "limit": _LIST_LIMIT}


_USER_SQL = f"""
SELECT u.id, u.student_id, u.display_name, u.locale, u.created_at,
       {_ACCOUNT_STATE} AS account, {_CLASS_REMINDERS_ON} AS class_reminders
FROM users u
LEFT JOIN external_accounts ea ON ea.user_id = u.id AND ea.provider = 'ntust_sso'
{_NOTIFICATION_DOC}
WHERE u.id = $1 AND u.deleted_at IS NULL
"""

_DEVICES_SQL = """
SELECT ud.id, ud.device_name, ud.device_model, ud.platform, ud.os_version, ud.app_version,
       ud.last_seen_at, ud.created_at, ud.cloud_sync_enabled, ud.bulletin_push_enabled,
       ud.sync_live_activity,
       (
           SELECT json_build_object('provider', t.provider, 'environment', t.environment)
           FROM device_push_tokens t
           WHERE t.device_id = ud.id AND t.status = 'active' AND t.token_kind = 'standard'
           ORDER BY t.id DESC LIMIT 1
       ) AS push_token
FROM user_devices ud
WHERE ud.user_id = $1 AND ud.deleted_at IS NULL
ORDER BY ud.last_seen_at DESC NULLS LAST, ud.created_at
"""

# The term the apps take as current (server/academic_calendar/terms.py), or
# while none is published the semester server/push/course_reminders.py falls back on.
_TERM_SQL = f"""
SELECT COALESCE(
    (SELECT code FROM semester_terms WHERE start_date <= {_TODAY}
     ORDER BY start_date DESC, code DESC LIMIT 1),
    (SELECT code FROM semester_terms ORDER BY start_date, code DESC LIMIT 1),
    (SELECT semester FROM user_courses
     WHERE user_id = $1 AND enrollment_status = 'enrolled'
       AND schedule_json NOT IN ('{{}}'::jsonb, '[]'::jsonb)
     ORDER BY (source = 'ntust_portal' OR semester ~ '^[0-9]{{3}}[12H]$') DESC,
              semester COLLATE "C" DESC
     LIMIT 1)
)
"""

_WEEK_SQL = f"SELECT {_TODAY} - (extract(isodow FROM {_TODAY})::int - 1)"

# Enrolled courses of the term, as course_reminders.py reads them.
_COURSES_SQL = """
SELECT c.id, c.course_name AS name, c.classroom, c.schedule_json AS schedule,
       COALESCE(
           array_agg(s.skipped_on ORDER BY s.skipped_on) FILTER (WHERE s.id IS NOT NULL),
           '{}'
       ) AS skipped
FROM user_courses c
LEFT JOIN user_course_skipped_dates s
    ON s.user_course_id = c.id AND s.deleted_at IS NULL
   AND s.skipped_on BETWEEN $3 AND $3 + 6
WHERE c.user_id = $1 AND c.semester = $2 AND c.enrollment_status = 'enrolled'
GROUP BY c.id
ORDER BY c.course_name, c.id
"""

_PUSH_AT = "COALESCE(pd.sent_at, pd.updated_at, j.cancelled_at, j.updated_at)"
# One row per delivery that settled, or per job that ended without one. Reminders
# are filed ahead of time, so the window follows when a push went out.
_PUSH_OUTCOMES = f"""
FROM push_jobs j
LEFT JOIN push_deliveries pd ON pd.push_job_id = j.id AND pd.status IN ('sent', 'failed')
WHERE j.user_id = $1
  AND {_PUSH_AT} > now() - interval '7 days'
  AND (pd.id IS NOT NULL OR j.status IN ('failed', 'cancelled'))
"""

_PUSHES_SQL = f"""
SELECT j.id AS job_id, pd.id AS delivery_id, j.channel, j.scenario, j.payload,
       COALESCE(pd.device_id, j.device_id) AS device_id,
       COALESCE(pd.status, j.status) AS status,
       {_PUSH_AT} AS at
{_PUSH_OUTCOMES}
ORDER BY at DESC, j.id DESC
LIMIT $2
"""

_PUSH_COUNTS_SQL = f"""
SELECT COALESCE(pd.status, j.status) AS status, count(*) AS pushes
{_PUSH_OUTCOMES}
GROUP BY 1
"""

# The page lists this many; `push_counts` still counts them all.
_PUSH_ROWS = 500

# A full sync leaves no row, so only a device's last upload can be shown.
_LAST_UPLOAD_SQL = """
SELECT created_at AS at, device_id FROM user_change_log
WHERE user_id = $1 AND device_id IS NOT NULL
ORDER BY revision DESC LIMIT 1
"""


def _push(row) -> dict:
    job = {"channel": row["channel"], "payload": payload_of(row["payload"])}
    return {
        "id": f"{row['job_id']}-{row['delivery_id'] or 0}",
        "at": row["at"],
        "channel": row["channel"],
        "scenario": row["scenario"],
        "title": summary(job)["title"],
        "device_id": row["device_id"],
        "status": row["status"],
    }


@router.get("/{user_id}")
async def get_user(request: Request, user_id: uuid.UUID, pool=Depends(get_pool)) -> dict:
    async with pool.acquire() as conn:
        user = await conn.fetchrow(_USER_SQL, user_id)
        if user is None:
            raise HTTPException(404, "User not found")
        devices = await conn.fetch(_DEVICES_SQL, user_id)
        term = await conn.fetchval(_TERM_SQL, user_id)
        week_start = await conn.fetchval(_WEEK_SQL)
        courses = await conn.fetch(_COURSES_SQL, user_id, term, week_start) if term else []
        pushes = await conn.fetch(_PUSHES_SQL, user_id, _PUSH_ROWS)
        counts = {r["status"]: r["pushes"] for r in await conn.fetch(_PUSH_COUNTS_SQL, user_id)}
        last_upload = await conn.fetchrow(_LAST_UPLOAD_SQL, user_id)
        await audit(conn, request, "user.view", user_id=user_id)
    return {
        **dict(user),
        "devices": [
            {**dict(d), "push_token": json.loads(d["push_token"]) if d["push_token"] else None}
            for d in devices
        ],
        "term": term,
        "week_start": week_start,
        "courses": [
            {**dict(c), "schedule": _json_list(c["schedule"])} for c in courses
        ],
        "pushes": [_push(p) for p in pushes],
        "push_counts": {k: counts.get(k, 0) for k in ("sent", "failed", "cancelled")},
        "last_upload": dict(last_upload) if last_upload else None,
    }


def _json_list(raw) -> list:
    """asyncpg hands JSONB back as text; a malformed upload or entry shows as no meeting."""
    value = json.loads(raw) if isinstance(raw, str) else raw
    return [e for e in value if isinstance(e, dict)] if isinstance(value, list) else []
