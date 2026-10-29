"""Semester dates and school holidays — the operator side of the feed the
apps read from `/v3/calendar/semesters`.

Reads/writes `semester_terms` and `academic_holidays` directly over
tigerduck-db, like every other portal route.

Semester codes are offered from what the database already knows about
(`user_courses.semester`, populated by every client upload) so the operator
picks a term that really exists rather than typing one, but a free-text code
is still accepted — a term can need dates before anyone has uploaded a
course under it.
"""
from __future__ import annotations

from datetime import date

import asyncpg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..audit import audit
from ..db import get_pool

router = APIRouter(prefix="/api/academic-calendar")


def _term_to_dict(r) -> dict:
    return {
        "code": r["code"],
        "start_date": r["start_date"].isoformat(),
        "end_date": r["end_date"].isoformat(),
        "name_zh": r["name_zh"],
        "name_en": r["name_en"],
    }


def _holiday_to_dict(r) -> dict:
    return {
        "id": r["id"],
        "name_zh": r["name_zh"],
        "name_en": r["name_en"],
        "start_date": r["start_date"].isoformat(),
        "end_date": r["end_date"].isoformat(),
    }


def _parse_range(body: dict) -> tuple[date, date] | JSONResponse:
    try:
        start = date.fromisoformat((body.get("start_date") or "").strip())
        end = date.fromisoformat((body.get("end_date") or "").strip())
    except ValueError:
        return JSONResponse(
            status_code=400, content={"detail": "start_date/end_date must be YYYY-MM-DD"}
        )
    if end < start:
        return JSONResponse(
            status_code=400, content={"detail": "end_date must not precede start_date"}
        )
    return start, end


@router.get("")
async def get_calendar(pool=Depends(get_pool)) -> JSONResponse:
    """Terms, holidays, and the terms the clients use that have no dates."""
    async with pool.acquire() as conn:
        terms = await conn.fetch(
            "SELECT code, start_date, end_date, name_zh, name_en "
            "FROM semester_terms ORDER BY code DESC"
        )
        holidays = await conn.fetch(
            "SELECT id, name_zh, name_en, start_date, end_date "
            "FROM academic_holidays ORDER BY start_date"
        )
        # Terms the clients have filed courses under but that have no dates
        # yet. Without dates the apps cannot place a semester boundary in the
        # calendar, so surfacing this is how an operator learns a new term
        # opened.
        undated = await conn.fetch(
            "SELECT DISTINCT c.semester AS code FROM user_courses c "
            "LEFT JOIN semester_terms t ON t.code = c.semester "
            "WHERE t.code IS NULL AND c.semester <> '' "
            "ORDER BY c.semester DESC"
        )
    return JSONResponse(
        content={
            "terms": [_term_to_dict(r) for r in terms],
            "holidays": [_holiday_to_dict(r) for r in holidays],
            "undated_terms": [r["code"] for r in undated],
        }
    )


@router.put("/terms/{code}")
async def upsert_term(code: str, request: Request, pool=Depends(get_pool)) -> JSONResponse:
    code = code.strip()
    if not code:
        return JSONResponse(status_code=400, content={"detail": "code required"})
    body = await request.json()
    parsed = _parse_range(body)
    if isinstance(parsed, JSONResponse):
        return parsed
    start, end = parsed
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "INSERT INTO semester_terms (code, start_date, end_date, name_zh, name_en) "
            "VALUES ($1,$2,$3,$4,$5) "
            "ON CONFLICT (code) DO UPDATE SET start_date=EXCLUDED.start_date, "
            "end_date=EXCLUDED.end_date, name_zh=EXCLUDED.name_zh, "
            "name_en=EXCLUDED.name_en, updated_at=now() "
            "RETURNING code, start_date, end_date, name_zh, name_en",
            code,
            start,
            end,
            (body.get("name_zh") or "").strip(),
            (body.get("name_en") or "").strip(),
        )
        await audit(conn, request, "calendar.term.upsert", code=code)
    return JSONResponse(content=_term_to_dict(row))


@router.delete("/terms/{code}")
async def delete_term(
    code: str, request: Request, pool=Depends(get_pool)
) -> JSONResponse:
    try:
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("DELETE FROM semester_terms WHERE code = $1", code)
            await audit(conn, request, "calendar.term.delete", code=code)
    except asyncpg.ForeignKeyViolationError:
        return JSONResponse(
            status_code=409,
            content={"detail": "delete the term's course changes first"},
        )
    return JSONResponse(content={"deleted": code})


@router.post("/holidays")
async def create_holiday(request: Request, pool=Depends(get_pool)) -> JSONResponse:
    body = await request.json()
    name_zh = (body.get("name_zh") or "").strip()
    name_en = (body.get("name_en") or "").strip()
    if not name_zh or not name_en:
        return JSONResponse(
            status_code=400,
            content={"detail": "name_zh and name_en are both required"},
        )
    parsed = _parse_range(body)
    if isinstance(parsed, JSONResponse):
        return parsed
    start, end = parsed
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "INSERT INTO academic_holidays (name_zh, name_en, start_date, end_date) "
            "VALUES ($1,$2,$3,$4) "
            "RETURNING id, name_zh, name_en, start_date, end_date",
            name_zh,
            name_en,
            start,
            end,
        )
        await audit(conn, request, "calendar.holiday.create", holiday_id=row["id"])
    return JSONResponse(content=_holiday_to_dict(row))


@router.put("/holidays/{holiday_id}")
async def update_holiday(
    holiday_id: int, request: Request, pool=Depends(get_pool)
) -> JSONResponse:
    body = await request.json()
    name_zh = (body.get("name_zh") or "").strip()
    name_en = (body.get("name_en") or "").strip()
    if not name_zh or not name_en:
        return JSONResponse(
            status_code=400,
            content={"detail": "name_zh and name_en are both required"},
        )
    parsed = _parse_range(body)
    if isinstance(parsed, JSONResponse):
        return parsed
    start, end = parsed
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "UPDATE academic_holidays SET name_zh=$2, name_en=$3, start_date=$4, "
            "end_date=$5, updated_at=now() WHERE id=$1 "
            "RETURNING id, name_zh, name_en, start_date, end_date",
            holiday_id,
            name_zh,
            name_en,
            start,
            end,
        )
        if row is not None:
            await audit(
                conn, request, "calendar.holiday.update", holiday_id=holiday_id
            )
    if row is None:
        return JSONResponse(status_code=404, content={"detail": "holiday not found"})
    return JSONResponse(content=_holiday_to_dict(row))


@router.delete("/holidays/{holiday_id}")
async def delete_holiday(
    holiday_id: int, request: Request, pool=Depends(get_pool)
) -> JSONResponse:
    """Deleting a holiday also drops every user's override for it.

    The `user_holiday_overrides` foreign key cascades, which is what we
    want: an override is "notify me anyway on *this* holiday", and it has no
    meaning once the holiday is gone.
    """
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("DELETE FROM academic_holidays WHERE id = $1", holiday_id)
        await audit(conn, request, "calendar.holiday.delete", holiday_id=holiday_id)
    return JSONResponse(content={"deleted": holiday_id})
