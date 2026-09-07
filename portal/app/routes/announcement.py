"""Announcement composer — operator authoring of in-app bulletins.

Reads/writes the `bulletins` table directly over tigerduck-db. A new
bulletin is born `processing_state='processed'`, so the backend's v3
`user_dispatch` tick (every ~60s) matches it against
`UserBulletinSubscription` rules and fans out `push_jobs` to subscribers
— no portal→backend call needed.

Replaces the old `/v2/bulletins*` proxy. The `/api/announcement/*`
contract the SPA consumes is unchanged.

Taxonomy is duplicated here (the portal can't import `server`); keep it in
sync with `server/bulletins/taxonomy.py` if the enums change.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..db import get_pool

router = APIRouter(prefix="/api/announcement")

# --- Taxonomy (mirror of server/bulletins/taxonomy.py) ---------------------
# (id, label) in enum-definition order so the picker renders consistently.
_ORGS: list[tuple[str, str]] = [
    ("department", "系院所"),
    ("academic_affairs", "教務處"),
    ("student_affairs", "學務處"),
    ("computer_center", "電算中心"),
    ("language_center", "語言中心"),
    ("bilingual_office", "雙語辦公室"),
    ("general_education", "通識中心"),
    ("general_affairs", "總務處"),
    ("hr", "人事室"),
    ("library", "圖書館"),
    ("pe", "體育室"),
    ("research", "研發處"),
    ("industry_academia", "產學處"),
    ("international", "國際處"),
    ("safety", "安全"),
    ("other", "其他"),
    ("server", "伺服器"),
]
_TAGS: list[tuple[str, str]] = [
    ("free_meal", "便當"),
    ("event", "講座"),
    ("scholarship", "獎助學金"),
    ("competition", "競賽"),
    ("course", "選課"),
    ("registration", "註冊"),
    ("housing", "宿舍"),
    ("exam", "考試"),
    ("facility", "維修"),
    ("payment", "繳費"),
    ("internship", "實習"),
    ("international_exchange", "國際"),
    ("forwarded", "轉發"),
    ("server_notification", "伺服器通知"),
]
_DEFAULT_TAGS = sorted(
    ["free_meal", "scholarship", "payment", "exam", "facility"]
)
_ORG_IDS = {o for o, _ in _ORGS}
_TAG_IDS = {t for t, _ in _TAGS}


@router.get("/taxonomy")
async def taxonomy() -> JSONResponse:
    return JSONResponse(
        content={
            "orgs": [{"id": o, "label": label} for o, label in _ORGS],
            "tags": [{"id": t, "label": label} for t, label in _TAGS],
            "default_tags": _DEFAULT_TAGS,
        }
    )


def _coerce_org(raw: str | None) -> str | None:
    return raw if raw in _ORG_IDS else None


def _coerce_tags(raw) -> list[str]:
    return [t for t in (raw or []) if t in _TAG_IDS]


def _summary(r) -> dict:
    return {
        "id": r["id"],
        "external_id": r["external_id"],
        "source": r["source"],
        "source_url": r["source_url"],
        "title": r["title"],
        "title_clean": r["title_clean"],
        "summary": r["summary"],
        "canonical_org": _coerce_org(r["canonical_org"]),
        "content_tags": _coerce_tags(r["content_tags"]),
        "importance": r["importance"],
        "posted_at": r["posted_at"].isoformat() if r["posted_at"] else None,
        "is_deleted": r["is_deleted"],
    }


def _detail(r) -> dict:
    return {
        **_summary(r),
        "body_clean": r["body_clean"],
        "body_md": r["body_md"],
        "raw_publisher": r["raw_publisher"],
    }


_COLS = (
    "id, external_id, source, source_url, title, title_clean, summary, "
    "canonical_org, content_tags, importance, posted_at, is_deleted, "
    "body_clean, body_md, raw_publisher"
)


@router.get("/list")
async def list_bulletins(
    pool=Depends(get_pool),
    limit: int = Query(default=30, ge=1, le=100),
    cursor: int | None = Query(default=None, ge=0),
    include_deleted: bool = Query(default=False),
) -> JSONResponse:
    # Keyset pagination over (posted_at DESC NULLS LAST, id DESC). Fetch one
    # extra to know whether a next page exists.
    where = ["canonical_org IS NOT NULL"]
    params: list = []
    async with pool.acquire() as conn:
        if cursor is not None:
            cur = await conn.fetchrow(
                "SELECT posted_at, id FROM bulletins WHERE id = $1", cursor
            )
            if cur is not None:
                if cur["posted_at"] is not None:
                    params.append(cur["posted_at"])
                    params.append(cur["id"])
                    where.append(
                        f"(posted_at < ${len(params) - 1} "
                        f"OR (posted_at = ${len(params) - 1} AND id < ${len(params)}) "
                        f"OR posted_at IS NULL)"
                    )
                else:
                    params.append(cur["id"])
                    where.append(f"(posted_at IS NULL AND id < ${len(params)})")
        if not include_deleted:
            where.append("is_deleted = false")
        params.append(limit + 1)
        sql = (
            f"SELECT {_COLS} FROM bulletins WHERE {' AND '.join(where)} "
            f"ORDER BY posted_at DESC NULLS LAST, id DESC LIMIT ${len(params)}"
        )
        rows = await conn.fetch(sql, *params)
    has_next = len(rows) > limit
    items = [_summary(r) for r in rows[:limit]]
    next_cursor = items[-1]["id"] if has_next and items else None
    return JSONResponse(content={"items": items, "next_cursor": next_cursor})


@router.get("/{bulletin_id}")
async def get_bulletin(bulletin_id: int, pool=Depends(get_pool)) -> JSONResponse:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {_COLS} FROM bulletins WHERE id = $1", bulletin_id
        )
    if row is None:
        raise HTTPException(status_code=404, detail="bulletin not found")
    return JSONResponse(content=_detail(row))


class _CreateSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=500)
    title_clean: str | None = Field(default=None, max_length=200)
    summary: str | None = Field(default=None, max_length=2000)
    body_clean: str | None = Field(default=None, max_length=20000)
    body_md: str | None = Field(default=None, max_length=20000)
    canonical_org: str = Field(min_length=1, max_length=32)
    content_tags: list[str] = Field(default_factory=list, max_length=8)
    importance: str = Field(default="normal", max_length=16)
    source_url: str = Field(
        default="https://announce.ntust.edu.tw/manual",
        min_length=1,
        max_length=1000,
    )


@router.post("")
async def create_bulletin(
    payload: _CreateSubmission, pool=Depends(get_pool)
) -> JSONResponse:
    if payload.canonical_org not in _ORG_IDS:
        raise HTTPException(status_code=422, detail="unknown canonical_org")
    tags = [t for t in payload.content_tags if t in _TAG_IDS]
    # Born processed so the v3 user_dispatch tick fans it out to subscribers.
    external_id = f"manual-{int(time.time() * 1000)}"
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            INSERT INTO bulletins (
                source, external_id, source_url, title, title_clean,
                summary, body_clean, body_md, canonical_org, content_tags,
                importance, posted_at, processing_state, processing_attempts,
                is_deleted
            ) VALUES (
                'manual', $1, $2, $3, $4, $5, $6, $7, $8, $9::varchar[],
                $10, now(), 'processed', 0, false
            )
            RETURNING {_COLS}
            """,
            external_id,
            payload.source_url,
            payload.title,
            payload.title_clean or payload.title,
            payload.summary,
            payload.body_clean,
            payload.body_md,
            payload.canonical_org,
            tags,
            payload.importance,
        )
    return JSONResponse(status_code=201, content=_detail(row))


class _UpdateSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=500)
    title_clean: str | None = Field(default=None, max_length=200)
    summary: str | None = Field(default=None, max_length=2000)
    body_clean: str | None = Field(default=None, max_length=20000)
    body_md: str | None = Field(default=None, max_length=20000)
    canonical_org: str | None = Field(default=None, max_length=32)
    content_tags: list[str] | None = Field(default=None, max_length=8)
    importance: str | None = Field(default=None, max_length=16)


# Columns the operator may patch. Required-ish ones are protected from an
# explicit null below.
_PROTECTED = {"title", "canonical_org", "content_tags", "importance"}


@router.patch("/{bulletin_id}")
async def update_bulletin(
    bulletin_id: int, request: Request, pool=Depends(get_pool)
) -> JSONResponse:
    # Only touch keys the operator actually sent (exclude_unset).
    submitted = _UpdateSubmission(**(await request.json()))
    data = submitted.model_dump(exclude_unset=True)
    if data.get("canonical_org") is not None and data["canonical_org"] not in _ORG_IDS:
        raise HTTPException(status_code=422, detail="unknown canonical_org")
    if data.get("content_tags") is not None:
        data["content_tags"] = [t for t in data["content_tags"] if t in _TAG_IDS]

    sets: list[str] = []
    params: list = []
    for key, value in data.items():
        if value is None and key in _PROTECTED:
            continue
        params.append(value)
        cast = "::varchar[]" if key == "content_tags" else ""
        sets.append(f"{key} = ${len(params)}{cast}")
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM bulletins WHERE id = $1", bulletin_id
        )
        if not exists:
            raise HTTPException(status_code=404, detail="bulletin not found")
        if sets:
            params.append(bulletin_id)
            await conn.execute(
                f"UPDATE bulletins SET {', '.join(sets)}, updated_at = now() "
                f"WHERE id = ${len(params)}",
                *params,
            )
        row = await conn.fetchrow(
            f"SELECT {_COLS} FROM bulletins WHERE id = $1", bulletin_id
        )
    return JSONResponse(content=_detail(row))
