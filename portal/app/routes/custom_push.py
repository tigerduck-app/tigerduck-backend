"""Custom push composer — operator one-off notifications.

Delivers through the real v3 pipeline: the portal resolves target devices
from the v3 tables and inserts one `push_jobs` row per device (channel
'custom'); the backend's push-pipeline worker (polls every few seconds) fans them
out to APNs/FCM. No portal→backend call.

`keeps_record` additionally stores a `bulletins` row for in-app history,
marked done in `bulletin_user_match_runs` so the subscription dispatcher
does NOT also fan it out (delivery already happened via push_jobs).

A one-row-per-send summary lands in `custom_push_sends` to drive the
"recent sends" view. Replaces the old `/v2/custom-push/*` proxy.

A course code narrows the send to the devices of the students enrolled in
that course in the current term, resolved when it is sent.
"""
from __future__ import annotations

import json
import secrets
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from ..audit import audit
from ..db import get_pool

router = APIRouter(prefix="/api/custom-push")

# Fallback for `user_devices` rows written before `device_class` existed.
# Those rows only say which platform they are, and for Apple that is three
# form factors under one value — so the mapping is class -> platform, and a
# legacy row matches whenever any class sharing its platform is selected.
_CLASS_TO_PLATFORM = {
    "iphone": "ios",
    "ipad": "ipados",
    "mac": "macos",
    "android": "android",
    "android_tablet": "android",
}
# The platform an anonymous registration (`device_registrations`) of each
# class carries.
_CLASS_TO_ANONYMOUS_PLATFORM = {
    "iphone": "apple",
    "ipad": "apple",
    "mac": "apple",
    "android": "android",
    "android_tablet": "android",
}


class _TargetFilter(BaseModel):
    target_classes: list[
        Literal["iphone", "ipad", "mac", "android", "android_tablet"]
    ] = Field(min_length=1)
    user_id: str | None = Field(default=None, max_length=64)
    device_id: str | None = Field(default=None, max_length=128)
    list_id: int | None = Field(default=None, ge=1)
    course_code: str | None = Field(default=None, max_length=16)

    @field_validator("course_code")
    @classmethod
    def _course_code(cls, v: str | None) -> str | None:
        if v is None:
            return None
        # Read as no course, a blank code would send to every student.
        if not v.strip():
            raise ValueError("course_code is blank")
        return v.strip().upper()


class _PreviewRequest(_TargetFilter):
    keeps_record: bool = False


class _SendRequest(_TargetFilter):
    title: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=2000)
    keeps_record: bool
    force_ring: bool


def build_custom_push_payload(
    *,
    title: str,
    body: str,
    force_ring: bool,
    notification_id: str | None = None,
    bulletin_id: int | None = None,
) -> dict:
    """The push_jobs payload for one custom push.

    `kind` and its id are what each app routes on: a popup needs its
    `notification_id` (the apps de-dupe shown popups by it), a recorded push
    the `bulletin_id` of the stored bulletin it opens. Android's FcmService
    drops a message that names neither, so without them a custom push
    reached every Android device and showed on none.
    """
    payload: dict = {"title": title, "body": body, "force_ring": force_ring}
    if bulletin_id is not None:
        payload.update(kind="custom_push_bulletin", bulletin_id=bulletin_id)
    else:
        payload.update(kind="custom_push_popup", notification_id=notification_id)
    return payload


# The term both apps take as current (`server.academic_calendar.terms.
# current_term_code`): the one begun most recently, else the earliest.
_CURRENT_TERM = """
SELECT coalesce(
    (SELECT code FROM semester_terms
     WHERE start_date <= (now() AT TIME ZONE 'Asia/Taipei')::date
     ORDER BY start_date DESC, code DESC LIMIT 1),
    (SELECT code FROM semester_terms ORDER BY start_date, code DESC LIMIT 1)
)
"""

# A course's takers are its enrolled rows in the term, as the class reminder
# scan reads them; its name comes from a school row first, then the latest.
_TAKES = (
    "semester = ${term} AND course_no = ${code} AND enrollment_status = 'enrolled'"
)
_COURSE = f"""
SELECT (array_agg(course_name ORDER BY (source = 'ntust_portal') DESC,
                  updated_at DESC))[1] AS name,
       count(DISTINCT user_id) AS students
FROM user_courses WHERE {_TAKES.format(term=1, code=2)}
"""


async def _course(conn, filt: _TargetFilter) -> tuple[str | None, dict | None]:
    """The current term, and the filter's course in it, None when nobody
    takes it."""
    term = await conn.fetchval(_CURRENT_TERM)
    if filt.course_code is None or term is None:
        return term, None
    row = await conn.fetchrow(_COURSE, term, filt.course_code)
    if not row["students"]:
        return term, None
    course = {"code": filt.course_code, "name": row["name"], "students": row["students"]}
    return term, course


def _targeting_where(filt: _TargetFilter, term: str | None) -> tuple[str, list]:
    """Build the shared WHERE clause + params for a target filter.

    A device is targetable when it's live, push-enabled, one of the
    requested form factors, and has an active 'standard' delivery token.

    Form factor comes from `device_class`, which is the only field that can
    tell an Android tablet from an Android phone — `platform` reports
    "android" for both. Rows registered before that column exists carry '',
    and are matched on `platform` instead; this mirrors the same two-branch
    rule `server.push.custom_push_targeting` applies to the signed-out
    half of a send, so both halves resolve the same audience.
    """
    classes = list(filt.target_classes)
    platforms = sorted(
        {_CLASS_TO_PLATFORM[c] for c in classes if c in _CLASS_TO_PLATFORM}
    )
    params: list = [classes, platforms]
    clauses = [
        "ud.deleted_at IS NULL",
        "ud.server_push_enabled = true",
        (
            "(ud.device_class = ANY($1::text[]) "
            "OR (coalesce(ud.device_class, '') = '' "
            "AND ud.platform = ANY($2::text[])))"
        ),
        (
            "EXISTS (SELECT 1 FROM device_push_tokens t "
            "WHERE t.device_id = ud.id AND t.token_kind = 'standard' "
            "AND t.status = 'active')"
        ),
    ]
    if filt.user_id is not None:
        params.append(filt.user_id)
        clauses.append(f"(u.student_id = ${len(params)} OR u.id::text = ${len(params)})")
    if filt.device_id is not None:
        params.append(filt.device_id)
        clauses.append(f"ud.id::text = ${len(params)}")
    if filt.list_id is not None:
        params.append(filt.list_id)
        clauses.append(
            f"EXISTS (SELECT 1 FROM device_list_members m "
            f"WHERE m.device_id = ud.id AND m.list_id = ${len(params)})"
        )
    if filt.course_code is not None:
        params += [term, filt.course_code]
        takes = _TAKES.format(term=len(params) - 1, code=len(params))
        clauses.append(
            f"EXISTS (SELECT 1 FROM user_courses c WHERE c.user_id = ud.user_id AND {takes})"
        )
    return " AND ".join(clauses), params


def _anonymous_where(filt: _TargetFilter) -> tuple[str, list] | None:
    """The WHERE clause + params for the anonymous devices a recorded push
    also reaches, repeating `server.push.custom_push_targeting`, which the
    bulletin dispatcher resolves them with. None for a device list or a
    course: they hold signed-in devices only."""
    if filt.list_id is not None or filt.course_code is not None:
        return None
    classes = list(filt.target_classes)
    platforms = sorted({_CLASS_TO_ANONYMOUS_PLATFORM[c] for c in classes})
    params: list = [classes, platforms]
    clauses = [
        "r.server_push_enabled = true",
        "r.linked_user_id IS NULL",
        (
            "((r.platform = 'apple' AND coalesce(r.device_token_hex, '') <> '') "
            "OR (r.platform = 'android' AND coalesce(r.pts_token_hex, '') <> ''))"
        ),
        (
            "(r.device_class = ANY($1::text[]) "
            "OR (r.device_class = '' AND r.platform = ANY($2::text[])))"
        ),
    ]
    if filt.user_id is not None:
        params.append(filt.user_id)
        clauses.append(f"r.user_id = ${len(params)}")
    if filt.device_id is not None:
        params.append(filt.device_id)
        clauses.append(f"r.device_id = ${len(params)}")
    return " AND ".join(clauses), params


def _by_class(rows, classes: list[str]) -> dict[str, int]:
    matched = {c: 0 for c in classes}
    for r in rows:
        n = int(r["n"])
        cls = r["device_class"]
        if cls in matched:
            matched[cls] += n
            continue
        # A row with no class is one of the classes sharing its platform,
        # but which one is unknowable. Credit it only when the selection
        # leaves no ambiguity; otherwise show it as its own line rather
        # than inflating whichever class happens to sort first.
        candidates = [
            c
            for c in matched
            if r["platform"]
            in (_CLASS_TO_PLATFORM.get(c), _CLASS_TO_ANONYMOUS_PLATFORM.get(c))
        ]
        if len(candidates) == 1:
            matched[candidates[0]] += n
        elif candidates:
            key = f"{r['platform']} (unspecified)"
            matched[key] = matched.get(key, 0) + n
    return {**matched, "total": sum(matched.values())}


@router.post("/preview")
async def preview(filt: _PreviewRequest, pool=Depends(get_pool)) -> JSONResponse:
    anonymous_rows = []
    async with pool.acquire() as conn:
        term, course = await _course(conn, filt)
        where, params = _targeting_where(filt, term)
        rows = await conn.fetch(
            f"SELECT coalesce(ud.device_class, '') AS device_class, "
            f"ud.platform AS platform, count(*) AS n "
            f"FROM user_devices ud JOIN users u ON u.id = ud.user_id "
            f"WHERE {where} GROUP BY 1, 2",
            *params,
        )
        anonymous = _anonymous_where(filt) if filt.keeps_record else None
        if anonymous is not None:
            where, params = anonymous
            anonymous_rows = await conn.fetch(
                f"SELECT r.device_class, r.platform, count(*) AS n "
                f"FROM device_registrations r WHERE {where} GROUP BY 1, 2",
                *params,
            )
    return JSONResponse(
        content={
            "matched": _by_class([*rows, *anonymous_rows], filt.target_classes),
            # The part of `matched` that no account is signed in on.
            "anonymous": _by_class(anonymous_rows, filt.target_classes),
            "term": term,
            "course": course,
        }
    )


@router.post("")
async def send(
    body: _SendRequest, request: Request, pool=Depends(get_pool)
) -> JSONResponse:
    request_id = secrets.token_hex(8)  # 16 chars
    kind = "record" if body.keeps_record else "popup"
    target_classes = ",".join(body.target_classes)
    bulletin_id: int | None = None

    async with pool.acquire() as conn:
        async with conn.transaction():
            term, course = await _course(conn, body)
            if body.course_code is not None and course is None:
                raise HTTPException(
                    status_code=422,
                    detail=f"no student takes {body.course_code} in {term}"
                    if term
                    else "no term is published",
                )
            where, params = _targeting_where(body, term)
            targets = await conn.fetch(
                f"SELECT ud.id::text AS device_id, ud.user_id::text AS user_id "
                f"FROM user_devices ud JOIN users u ON u.id = ud.user_id "
                f"WHERE {where}",
                *params,
            )
            matched = len(targets)

            await conn.execute(
                """
                INSERT INTO custom_push_sends
                    (request_id, title, kind, target_classes, total)
                VALUES ($1, $2, $3, $4, $5)
                """,
                request_id,
                body.title,
                kind,
                target_classes,
                matched,
            )

            if body.keeps_record:
                # Stored for in-app history; the match-run row stops the
                # subscription dispatcher from re-sending it, and for a course
                # `notified_at` stops the anonymous one, which has no courses.
                bulletin_id = await conn.fetchval(
                    """
                    INSERT INTO bulletins (
                        source, external_id, source_url, title, title_clean,
                        summary, body_clean, canonical_org, content_tags,
                        importance, posted_at, processing_state,
                        processing_attempts, is_deleted, dispatch_filter_json,
                        notified_at
                    ) VALUES (
                        'custom_push', $1, '', $2, $2, $3, $3, 'server',
                        '{server_notification}'::varchar[], 'normal', now(),
                        'processed', 0, false, $4::jsonb,
                        CASE WHEN $5::bool THEN now() END
                    )
                    RETURNING id
                    """,
                    f"custom-{request_id}",
                    body.title,
                    body.body,
                    json.dumps(
                        {
                            "target_classes": list(body.target_classes),
                            "user_id": body.user_id,
                            "device_id": body.device_id,
                            "list_id": body.list_id,
                            "course_code": body.course_code,
                            "term": term if course else None,
                            "force_ring": body.force_ring,
                        }
                    ),
                    course is not None,
                )
                await conn.execute(
                    "INSERT INTO bulletin_user_match_runs (bulletin_id) "
                    "VALUES ($1) ON CONFLICT DO NOTHING",
                    bulletin_id,
                )

            payload = json.dumps(
                build_custom_push_payload(
                    title=body.title,
                    body=body.body,
                    force_ring=body.force_ring,
                    notification_id=None if body.keeps_record else request_id,
                    bulletin_id=bulletin_id,
                )
            )
            if targets:
                await conn.executemany(
                    """
                    INSERT INTO push_jobs (
                        user_id, device_id, dedupe_key, channel, scenario,
                        fire_at, payload, status
                    ) VALUES (
                        $1::uuid, $2::uuid, $3, 'custom', 'custom',
                        now(), $4::jsonb, 'pending'
                    )
                    ON CONFLICT DO NOTHING
                    """,
                    [
                        (
                            t["user_id"],
                            t["device_id"],
                            f"custom:{request_id}:{t['device_id']}",
                            payload,
                        )
                        for t in targets
                    ],
                )
            # A user or device filter matches one account or device.
            first = targets[0] if targets else None
            await audit(
                conn,
                request,
                "push.custom.send",
                user_id=first["user_id"] if first and body.user_id else None,
                device_id=first["device_id"] if first and body.device_id else None,
                request_id=request_id,
                kind=kind,
                target_classes=list(body.target_classes),
                list_id=body.list_id,
                course_code=body.course_code,
                term=term if course else None,
                force_ring=body.force_ring,
                matched=matched,
            )
    return JSONResponse(
        content={
            "request_id": request_id,
            "kind": kind,
            "matched": matched,
            "queued": matched,
        }
    )


@router.get("/recent")
async def recent(pool=Depends(get_pool), limit: int = 30) -> JSONResponse:
    limit = max(1, min(limit, 100))
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT s.request_id, s.title, s.kind, s.target_classes, s.total,
                   s.created_at,
                   EXISTS (
                       SELECT 1 FROM push_jobs j
                       WHERE j.dedupe_key LIKE 'custom:' || s.request_id || ':%'
                         AND j.status = 'pending'
                   ) AS is_queueing
            FROM custom_push_sends s
            ORDER BY s.created_at DESC
            LIMIT $1
            """,
            limit,
        )
    return JSONResponse(
        content=[
            {
                "id": r["request_id"],
                "kind": r["kind"],
                "title": r["title"],
                "target_classes": r["target_classes"].split(",")
                if r["target_classes"]
                else [],
                "total": int(r["total"] or 0),
                "sent_at": r["created_at"].isoformat() if r["created_at"] else None,
                "is_queueing": r["is_queueing"],
            }
            for r in rows
        ]
    )
