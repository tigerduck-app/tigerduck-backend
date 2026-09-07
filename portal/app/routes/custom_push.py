"""Custom push composer — operator one-off notifications.

Delivers through the real v3 pipeline: the portal resolves target devices
from the v3 tables and inserts one `push_jobs` row per device (channel
'custom'); the backend's push-pipeline worker (polls every ~30s) fans them
out to APNs/FCM, honouring each token's dev/prod environment. No
portal→backend call.

`keeps_record` additionally stores a `bulletins` row for in-app history,
marked done in `bulletin_user_match_runs` so the subscription dispatcher
does NOT also fan it out (delivery already happened via push_jobs).

A one-row-per-send summary lands in `custom_push_sends` to drive the
"recent sends" view. Replaces the old `/v2/custom-push/*` proxy.
"""
from __future__ import annotations

import json
import secrets
from typing import Literal

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..db import get_pool

router = APIRouter(prefix="/api/custom-push")

_CLASS_TO_PLATFORM = {"iphone": "ios", "ipad": "ipados", "android": "android"}
_PLATFORM_TO_CLASS = {"ios": "iphone", "ipados": "ipad", "android": "android"}


class _TargetFilter(BaseModel):
    target_classes: list[Literal["iphone", "ipad", "android"]] = Field(min_length=1)
    user_id: str | None = Field(default=None, max_length=64)
    device_id: str | None = Field(default=None, max_length=128)
    list_id: int | None = Field(default=None, ge=1)


class _SendRequest(_TargetFilter):
    title: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=2000)
    keeps_record: bool
    force_ring: bool


def _targeting_where(filt: _TargetFilter) -> tuple[str, list]:
    """Build the shared WHERE clause + params for a target filter.

    A device is targetable when it's live, push-enabled, on one of the
    requested platforms, and has an active 'standard' delivery token.
    """
    platforms = [
        _CLASS_TO_PLATFORM[c] for c in filt.target_classes if c in _CLASS_TO_PLATFORM
    ]
    params: list = [platforms]
    clauses = [
        "ud.deleted_at IS NULL",
        "ud.server_push_enabled = true",
        "ud.platform = ANY($1::text[])",
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
    return " AND ".join(clauses), params


@router.post("/preview")
async def preview(filt: _TargetFilter, pool=Depends(get_pool)) -> JSONResponse:
    where, params = _targeting_where(filt)
    sql = (
        f"SELECT ud.platform AS platform, count(*) AS n "
        f"FROM user_devices ud JOIN users u ON u.id = ud.user_id "
        f"WHERE {where} GROUP BY ud.platform"
    )
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    matched = {c: 0 for c in filt.target_classes}
    for r in rows:
        cls = _PLATFORM_TO_CLASS.get(r["platform"])
        if cls in matched:
            matched[cls] += int(r["n"])
    return JSONResponse(content={"matched": matched})


@router.post("")
async def send(body: _SendRequest, pool=Depends(get_pool)) -> JSONResponse:
    where, params = _targeting_where(body)
    sql = (
        f"SELECT ud.id::text AS device_id, ud.user_id::text AS user_id "
        f"FROM user_devices ud JOIN users u ON u.id = ud.user_id "
        f"WHERE {where}"
    )
    request_id = secrets.token_hex(8)  # 16 chars
    kind = "record" if body.keeps_record else "popup"
    target_classes = ",".join(body.target_classes)
    payload = json.dumps(
        {"title": body.title, "body": body.body, "force_ring": body.force_ring}
    )

    async with pool.acquire() as conn:
        async with conn.transaction():
            targets = await conn.fetch(sql, *params)
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
                # subscription dispatcher from re-sending it.
                bulletin_id = await conn.fetchval(
                    """
                    INSERT INTO bulletins (
                        source, external_id, source_url, title, title_clean,
                        summary, body_clean, canonical_org, content_tags,
                        importance, posted_at, processing_state,
                        processing_attempts, is_deleted, dispatch_filter_json
                    ) VALUES (
                        'custom_push', $1, '', $2, $2, $3, $3, 'server',
                        '{server_notification}'::varchar[], 'normal', now(),
                        'processed', 0, false, $4::jsonb
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
                            "force_ring": body.force_ring,
                        }
                    ),
                )
                await conn.execute(
                    "INSERT INTO bulletin_user_match_runs (bulletin_id) "
                    "VALUES ($1) ON CONFLICT DO NOTHING",
                    bulletin_id,
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
