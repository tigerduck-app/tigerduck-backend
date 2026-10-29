"""Course changes: the school's cancelled classes, room and time changes and
notes for one course in a term (tigerduck-backend#177), in `course_changes`
(model: server/academic_calendar/models.py).

A change covers one class day, or every class day of a range but the ISO
weekdays it leaves out. A class day is a day of the term, not a holiday, on
which the course meets by its `user_courses.schedule_json`.
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse

from ..audit import audit
from ..db import get_pool

router = APIRouter(prefix="/api/course-changes")

KINDS = ("cancelled", "room", "start", "end", "note")
# The columns a request sets, in the order the INSERT and UPDATE bind them.
_FIELDS = (
    "kind", "mode", "start_date", "end_date", "excluded_weekdays", "periods",
    "room", "start_time", "end_time", "note_zh", "note_en",
)
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_HHMM = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")
_ROOM_MAX = 128

_CHANGE = """
SELECT id, term_code, course_no, kind, mode, start_date, end_date,
       excluded_weekdays, periods, room, start_time, end_time, note_zh, note_en,
       created_at, created_by, updated_at, updated_by
FROM course_changes
"""

# Enrolled rows with a course number, as the class reminder scan reads them
# (server/push/course_reminders.py).
_COURSES = """
WITH taken AS (
    SELECT * FROM user_courses
    WHERE semester = $1 AND course_no IS NOT NULL AND enrollment_status = 'enrolled'
), latest AS (
    SELECT DISTINCT ON (course_no) course_no, course_name, course_name_en,
           instructors, credits, classroom, classroom_map, schedule_json
    FROM taken
    ORDER BY course_no, (source = 'ntust_portal') DESC, updated_at DESC
), reach AS (
    SELECT t.course_no, count(DISTINCT t.user_id) AS students,
           count(DISTINCT d.id) AS devices
    FROM taken t
    LEFT JOIN user_devices d ON d.user_id = t.user_id AND d.deleted_at IS NULL
    GROUP BY t.course_no
), changes AS (
    SELECT course_no, count(*) AS n FROM course_changes
    WHERE term_code = $1 GROUP BY course_no
)
SELECT l.*, r.students, r.devices, coalesce(c.n, 0) AS changes
FROM latest l
JOIN reach r USING (course_no)
LEFT JOIN changes c USING (course_no)
ORDER BY l.course_no
"""


# A course's times, from the row the courses list shows.
_SCHEDULE = """
SELECT schedule_json FROM user_courses
WHERE semester = $1 AND course_no = $2 AND enrollment_status = 'enrolled'
ORDER BY (source = 'ntust_portal') DESC, updated_at DESC
LIMIT 1
"""


def _bad(detail: str, status: int = 400) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail})


def _period_key(period: str) -> tuple:
    # NTUST's order: 1-10, then the evening's A-D.
    return (not period.isdigit(), int(period) if period.isdigit() else 0, period)


def _meetings(schedule_json: str) -> dict[int, set[str]]:
    """ISO weekday -> the periods the course meets that day."""
    meets: dict[int, set[str]] = {}
    for entry in json.loads(schedule_json):
        if not isinstance(entry, dict) or not isinstance(entry.get("periods"), list):
            continue
        day = entry.get("day")
        if type(day) is int and 1 <= day <= 7:
            meets.setdefault(day, set()).update(str(p) for p in entry["periods"])
    return {day: periods for day, periods in meets.items() if periods}


def _change_to_dict(r) -> dict:
    single = r["mode"] == "day"
    return {
        "id": r["id"],
        "term": r["term_code"],
        "course_no": r["course_no"],
        "kind": r["kind"],
        "mode": r["mode"],
        "day": r["start_date"].isoformat() if single else None,
        "from": None if single else r["start_date"].isoformat(),
        "to": None if single else r["end_date"].isoformat(),
        "excluded_weekdays": list(r["excluded_weekdays"]),
        "periods": r["periods"],
        "room": r["room"],
        "start": r["start_time"],
        "end": r["end_time"],
        "note_zh": r["note_zh"],
        "note_en": r["note_en"],
        "created_at": r["created_at"].isoformat(),
        "created_by": r["created_by"],
        "updated_at": r["updated_at"].isoformat(),
        "updated_by": r["updated_by"],
    }


def _date(value) -> date | None:
    """A YYYY-MM-DD day; `fromisoformat` alone also takes 20261105 and week dates."""
    if not isinstance(value, str) or not _DAY.fullmatch(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _text(value) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _class_days(first, last, excluded, term, holidays, meets) -> list[date]:
    """The days from `first` to `last`, both inclusive, the course has class on."""
    days = []
    day = max(first, term["start_date"])
    while day <= min(last, term["end_date"]):
        weekday = day.isoweekday()
        if (
            weekday in meets
            and weekday not in excluded
            and not any(h["start_date"] <= day <= h["end_date"] for h in holidays)
        ):
            days.append(day)
        day += timedelta(days=1)
    return days


def _dates(body: dict, term) -> tuple[str, date, date, list[int]] | JSONResponse:
    mode = body.get("mode")
    if mode == "day":
        first = last = _date(body.get("day"))
        if first is None:
            return _bad("day must be YYYY-MM-DD")
        excluded: list[int] = []
    elif mode == "range":
        first, last = _date(body.get("from")), _date(body.get("to"))
        if first is None or last is None:
            return _bad("from and to must be YYYY-MM-DD")
        if last < first:
            return _bad("to must not precede from")
        raw = body.get("excluded_weekdays")
        raw = [] if raw is None else raw
        if not isinstance(raw, list) or any(type(d) is not int or not 1 <= d <= 7 for d in raw):
            return _bad("excluded_weekdays must be ISO weekdays, 1 (Monday) to 7 (Sunday)")
        excluded = sorted(set(raw))
    else:
        return _bad("mode must be day or range")
    for day in (first, last):
        if not term["start_date"] <= day <= term["end_date"]:
            return _bad(f"{day} is outside the term")
    return mode, first, last, excluded


def _values(body: dict, kind: str) -> dict | JSONResponse:
    """The new room, time or note; the dialog sends every field, so only the
    kind's own are read."""
    values = dict.fromkeys(("room", "start_time", "end_time", "note_zh", "note_en"))
    if kind == "room":
        values["room"] = _text(body.get("room"))
        if values["room"] is None or len(values["room"]) > _ROOM_MAX:
            return _bad(f"room required, at most {_ROOM_MAX} characters")
    elif kind in ("start", "end"):
        time = body.get(kind)
        if not isinstance(time, str) or not _HHMM.fullmatch(time):
            return _bad(f"{kind} must be HH:mm")
        values[f"{kind}_time"] = time
    elif kind == "note":
        values["note_zh"] = _text(body.get("note_zh"))
        if values["note_zh"] is None:
            return _bad("note_zh required")
        values["note_en"] = _text(body.get("note_en"))
    return values


def _parse(body: dict, term, holidays, meets) -> tuple[dict, list[date]] | JSONResponse:
    """The columns of the change `body` describes, and its class days."""
    kind = body.get("kind")
    if kind not in KINDS:
        return _bad(f"kind must be one of {', '.join(KINDS)}")
    values = _values(body, kind)
    if isinstance(values, JSONResponse):
        return values
    dates = _dates(body, term)
    if isinstance(dates, JSONResponse):
        return dates
    mode, first, last, excluded = dates
    days = _class_days(first, last, excluded, term, holidays, meets)
    if not days:
        if mode == "range":
            return _bad("the range covers no class day of the course")
        if any(h["start_date"] <= first <= h["end_date"] for h in holidays):
            return _bad(f"{first} is a holiday")
        return _bad(f"the course has no class on {first}")
    periods = body.get("periods")
    if periods is not None:
        if not isinstance(periods, list) or not periods:
            return _bad("periods must be a non-empty list, or absent for the whole day")
        periods = sorted({str(p) for p in periods}, key=_period_key)
        for day in days:
            if missing := set(periods) - meets[day.isoweekday()]:
                listed = ", ".join(sorted(missing, key=_period_key))
                return _bad(f"the course has no class in period {listed} on {day}")
    return {
        "kind": kind,
        "mode": mode,
        "start_date": first,
        "end_date": last,
        "excluded_weekdays": excluded,
        "periods": periods,
        **values,
    }, days


async def _check(
    conn, term: str, course_no: str, body: dict, change_id: int | None = None
) -> dict | JSONResponse:
    """Checks `body` against the term, its holidays, the course's times and
    the course's other changes of the kind.

    Holds a lock on the course until the transaction ends, so two writes
    cannot both pass the clash check.
    """
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        f"course_changes|{term}|{course_no}",
    )
    term_row = await conn.fetchrow(
        "SELECT start_date, end_date FROM semester_terms WHERE code = $1", term
    )
    if term_row is None:
        return _bad("term not found", 404)
    schedule = await conn.fetchval(_SCHEDULE, term, course_no)
    meets = _meetings(schedule) if schedule else {}
    if not meets:
        return _bad(f"no student has {course_no} with class times in {term}", 404)
    holidays = await conn.fetch(
        "SELECT start_date, end_date FROM academic_holidays"
        " WHERE end_date >= $1 AND start_date <= $2",
        term_row["start_date"],
        term_row["end_date"],
    )
    parsed = _parse(body, term_row, holidays, meets)
    if isinstance(parsed, JSONResponse):
        return parsed
    columns, days = parsed
    periods = set(columns["periods"] or ())
    others = await conn.fetch(
        "SELECT start_date, end_date, excluded_weekdays, periods FROM course_changes"
        " WHERE term_code = $1 AND course_no = $2 AND kind = $3"
        " AND id IS DISTINCT FROM $4",
        term,
        course_no,
        columns["kind"],
        change_id,
    )
    # Changes of a kind clash on a shared class day unless both name periods
    # and none in common: a course can meet twice a day.
    clashes = [
        day
        for other in others
        if not (periods and other["periods"] and periods.isdisjoint(other["periods"]))
        for day in _class_days(
            other["start_date"], other["end_date"], other["excluded_weekdays"],
            term_row, holidays, meets,
        )
        if day in days
    ]
    if clashes:
        day = min(clashes).isoformat()
        return JSONResponse(
            status_code=409,
            content={"detail": f"{day} already has a {columns['kind']} change", "day": day},
        )
    return columns


@router.get("/terms/{term}/courses")
async def list_courses(term: str, pool=Depends(get_pool)) -> JSONResponse:
    """The term's courses as students took them, for the Courses table."""
    async with pool.acquire() as conn:
        if not await conn.fetchval("SELECT 1 FROM semester_terms WHERE code = $1", term):
            return _bad("term not found", 404)
        rows = await conn.fetch(_COURSES, term)
    return JSONResponse(
        content={
            "term": term,
            "courses": [
                {
                    "course_no": r["course_no"],
                    "name": r["course_name"],
                    "name_en": r["course_name_en"],
                    "instructors": list(r["instructors"]),
                    "credits": None if r["credits"] is None else float(r["credits"]),
                    "classroom": r["classroom"],
                    "classroom_map": json.loads(r["classroom_map"]),
                    "meetings": [
                        {"day": day, "periods": sorted(periods, key=_period_key)}
                        for day, periods in sorted(_meetings(r["schedule_json"]).items())
                    ],
                    "students": r["students"],
                    "devices": r["devices"],
                    "changes": r["changes"],
                }
                for r in rows
            ],
        }
    )


@router.get("/terms/{term}/changes")
async def list_changes(
    term: str, pool=Depends(get_pool), course_no: str | None = None
) -> JSONResponse:
    """The term's changes by first day, or one course's."""
    async with pool.acquire() as conn:
        if not await conn.fetchval("SELECT 1 FROM semester_terms WHERE code = $1", term):
            return _bad("term not found", 404)
        rows = await conn.fetch(
            _CHANGE + " WHERE term_code = $1 AND ($2::text IS NULL OR course_no = $2)"
            " ORDER BY start_date, course_no, id",
            term,
            course_no,
        )
    return JSONResponse(content={"term": term, "changes": [_change_to_dict(r) for r in rows]})


@router.post("/terms/{term}/changes")
async def create_change(
    term: str, request: Request, pool=Depends(get_pool), body: dict = Body(...)
) -> JSONResponse:
    course_no = _text(body.get("course_no"))
    if course_no is None:
        return _bad("course_no required")
    async with pool.acquire() as conn, conn.transaction():
        parsed = await _check(conn, term, course_no, body)
        if isinstance(parsed, JSONResponse):
            return parsed
        row = await conn.fetchrow(
            f"INSERT INTO course_changes (term_code, course_no, {', '.join(_FIELDS)},"
            " created_by, updated_by) VALUES ($1, $2, "
            + ", ".join(f"${i}" for i in range(3, len(_FIELDS) + 3))
            + f", ${len(_FIELDS) + 3}, ${len(_FIELDS) + 3}) RETURNING id",
            term,
            course_no,
            *(parsed[f] for f in _FIELDS),
            request.state.actor,
        )
        change = await conn.fetchrow(_CHANGE + " WHERE id = $1", row["id"])
        await audit(
            conn, request, "course_change.create",
            change_id=row["id"], term=term, course_no=course_no, kind=parsed["kind"],
        )
    return JSONResponse(content=_change_to_dict(change))


@router.put("/changes/{change_id}")
async def update_change(
    change_id: int, request: Request, pool=Depends(get_pool), body: dict = Body(...)
) -> JSONResponse:
    """Replaces the change; its term and course stay."""
    async with pool.acquire() as conn, conn.transaction():
        current = await conn.fetchrow(_CHANGE + " WHERE id = $1 FOR UPDATE", change_id)
        if current is None:
            return _bad("change not found", 404)
        parsed = await _check(
            conn, current["term_code"], current["course_no"], body, change_id
        )
        if isinstance(parsed, JSONResponse):
            return parsed
        await conn.execute(
            "UPDATE course_changes SET "
            + ", ".join(f"{f} = ${i}" for i, f in enumerate(_FIELDS, start=2))
            + f", updated_by = ${len(_FIELDS) + 2}, updated_at = now() WHERE id = $1",
            change_id,
            *(parsed[f] for f in _FIELDS),
            request.state.actor,
        )
        change = await conn.fetchrow(_CHANGE + " WHERE id = $1", change_id)
        await audit(
            conn, request, "course_change.update",
            change_id=change_id, term=current["term_code"],
            course_no=current["course_no"], kind=parsed["kind"],
        )
    return JSONResponse(content=_change_to_dict(change))


@router.delete("/changes/{change_id}")
async def delete_change(
    change_id: int, request: Request, pool=Depends(get_pool)
) -> JSONResponse:
    # Hard delete: the feed sends a term's changes whole, so a change it
    # leaves out is gone; admin_audit_log keeps who deleted what.
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "DELETE FROM course_changes WHERE id = $1 RETURNING term_code, course_no, kind",
            change_id,
        )
        if row is None:
            return _bad("change not found", 404)
        await audit(
            conn, request, "course_change.delete",
            change_id=change_id, term=row["term_code"],
            course_no=row["course_no"], kind=row["kind"],
        )
    return JSONResponse(content={"deleted": change_id})
