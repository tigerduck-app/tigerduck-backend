"""Course changes: the school's cancelled classes, room and time changes,
assessments, online classes and notes for one course in a term
(tigerduck-backend#177), in `course_changes`
(model: server/academic_calendar/models.py).

A change covers one class day, or every class day of a range but the ISO
weekdays it leaves out. A class day is a day of the term, not a holiday, on
which the course meets by its `user_courses.schedule_json`; the database's
`course_class_days` (server/academic_calendar/models.py) works them out, and its
`course_change_violations` checks a change against the course's class times and
its other changes.

A change can come from a course announcement's review item: saving it with the
item's id approves the item, and editing or deleting a change an item wrote
records that on the item (course_announcements.py).
"""
from __future__ import annotations

import json
import re
from datetime import date

import asyncpg
from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse

from ..audit import audit
from ..db import get_pool

router = APIRouter(prefix="/api/course-changes")

KINDS = ("cancelled", "room", "start", "end", "assessment", "online", "note")
# The columns a request sets, in the order the INSERT and UPDATE bind them.
_FIELDS = (
    "kind", "mode", "start_date", "end_date", "excluded_weekdays", "periods",
    "room", "start_time", "end_time", "note_zh", "note_en",
)
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_HHMM = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")
_ROOM_MAX = 128
# What Postgres text cannot hold: a NUL, or a lone surrogate, which UTF-8 cannot encode.
_UNHELD = re.compile(r"[\x00\ud800-\udfff]")
# The largest id a bigint holds; asyncpg refuses a larger int.
BIGINT_MAX = 2**63 - 1

# The term's enrolled rows with a course number, as the class reminder scan reads
# them, and each course's row (`term_courses`, server/academic_calendar/models.py).
_LATEST = """
WITH taken AS (
    SELECT * FROM user_courses
    WHERE semester = $1 AND course_no IS NOT NULL AND enrollment_status = 'enrolled'
), latest AS (
    SELECT * FROM term_courses WHERE semester = $1
)
"""

_COURSES = _LATEST + """, reach AS (
    SELECT t.course_no, count(DISTINCT t.user_id) AS students,
           count(DISTINCT d.id) AS devices
    FROM taken t
    LEFT JOIN user_devices d ON d.user_id = t.user_id AND d.deleted_at IS NULL
    GROUP BY t.course_no
), changes AS (
    SELECT course_no, count(*) AS n FROM course_changes
    WHERE term_code = $1 GROUP BY course_no
)
SELECT l.course_no, l.course_name, l.course_name_en, l.instructors, l.credits,
       l.classroom,
       CASE jsonb_typeof(l.classroom_map) WHEN 'object' THEN l.classroom_map
       ELSE '{}' END AS classroom_map,
       (SELECT coalesce(jsonb_agg(jsonb_build_object('day', weekday, 'periods', periods)
                                  ORDER BY weekday), '[]')
        FROM course_meetings(l.schedule_json)) AS meetings,
       r.students, r.devices, coalesce(c.n, 0) AS changes
FROM latest l
JOIN reach r USING (course_no)
LEFT JOIN changes c USING (course_no)
ORDER BY l.course_no
"""

# The term's changes, each with the class days today's calendar leaves it.
_CHANGES = _LATEST + """
SELECT c.*, course_class_days(c.term_code, l.schedule_json, c.start_date,
                              c.end_date, c.excluded_weekdays, c.periods) AS class_days
FROM course_changes c
LEFT JOIN latest l USING (course_no)
WHERE c.term_code = $1
"""

# The candidate's columns in the order course_change_violations takes them,
# up to its periods.
_CANDIDATE = ("kind", "mode", "start_date", "end_date", "excluded_weekdays")
_VIOLATIONS = (
    "SELECT * FROM course_change_violations($1, $2, $3, $4, $5, $6, $7, $8, $9) ORDER BY day"
)

# A review item with its announcement (server/course_announcements/models.py),
# locked. `current`: its extraction is of the announcement's stored version and
# no later extraction of the announcement has finished.
_ITEM = """
SELECT i.id, i.course_no, i.action, i.payload, i.course_change_id, i.state,
       a.id AS announcement_id, a.term_code,
       e.version = a.modified_at AND NOT EXISTS (
           SELECT 1 FROM course_announcement_extractions AS later
           WHERE later.announcement_id = a.id AND later.status = 'done' AND later.id > e.id
       ) AS current
FROM course_announcement_items AS i
JOIN course_announcement_extractions AS e ON e.id = i.extraction_id
JOIN course_announcements AS a ON a.id = e.announcement_id
WHERE i.id = $1
FOR UPDATE OF i
"""


class _Refusal(JSONResponse):
    # ASCII JSON, as for the backend's 422 (server/main.py): a lone surrogate
    # a request's JSON carried would fail to encode as UTF-8.
    def render(self, content) -> bytes:
        return json.dumps(content).encode()


def _bad(detail: str, status: int = 400) -> JSONResponse:
    return _Refusal(status_code=status, content={"detail": detail})


def _period_key(period: str) -> tuple:
    # NTUST's order: 1-10, then the evening's A-D. isdigit() alone lets in
    # digits such as "\u00b2" that int() rejects.
    numeric = period.isascii() and period.isdigit()
    return (not numeric, int(period) if numeric else 0, period)


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
        "class_days": [day.isoformat() for day in r["class_days"]],
        # No class day left after a term or holiday edit.
        "stale": not r["class_days"],
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
    """The new room or time, the note, the assessment's names or the online
    class's description; the dialog sends every field, so only the kind's own
    are read."""
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
    elif kind in ("assessment", "online", "note"):
        values["note_zh"] = _text(body.get("note_zh"))
        values["note_en"] = _text(body.get("note_en"))
        if kind == "assessment" and None in (values["note_zh"], values["note_en"]):
            return _bad("note_zh and note_en required")
        if values["note_zh"] is None and (kind == "note" or values["note_en"] is not None):
            return _bad("note_zh required")
    return values


def _parse(body: dict, term) -> dict | JSONResponse:
    """The columns of the change `body` describes."""
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
    periods = body.get("periods")
    if periods is not None:
        if not isinstance(periods, list) or not periods:
            return _bad("periods must be a non-empty list, or absent for the whole day")
        periods = sorted({str(p) for p in periods}, key=_period_key)
    return {
        "kind": kind,
        "mode": mode,
        "start_date": first,
        "end_date": last,
        "excluded_weekdays": excluded,
        "periods": periods,
        **values,
    }


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
        # Blocks a term delete until this write commits, so the delete's
        # foreign key check sees the change and answers 409.
        "SELECT start_date, end_date FROM semester_terms WHERE code = $1 FOR KEY SHARE",
        term,
    )
    if term_row is None:
        return _bad("term not found", 404)
    columns = _parse(body, term_row)
    parsed = not isinstance(columns, JSONResponse)
    periods = columns["periods"] if parsed else None
    # The course has no class in a period Postgres cannot hold, so the function
    # takes NULL, which no list of periods contains, in its place.
    unheld = {p for p in periods or () if _UNHELD.search(p)}
    # The first day each rule is broken on. A body that does not parse still
    # asks: a course without class times is refused before the body's format.
    broken = {}
    for row in await conn.fetch(
        _VIOLATIONS,
        term,
        course_no,
        *(columns[f] if parsed else None for f in _CANDIDATE),
        periods and [None if p in unheld else p for p in periods],
        change_id,
    ):
        broken.setdefault(row["code"], row)
    if "no_meetings" in broken:
        if change_id is None:
            return _bad(f"no student has {course_no} with class times in {term}", 404)
        return _bad(
            f"no student has {course_no} with class times in {term} any more,"
            " so the change can only be deleted",
            409,
        )
    if not parsed:
        return columns
    first = columns["start_date"]
    if "no_class_day" in broken:
        if columns["mode"] == "range":
            return _bad("the range covers no class day of the course")
        return _bad(f"the course has no class on {first}")
    if "holiday" in broken:
        return _bad(f"{first} is a holiday")
    if missing := broken.get("missing_periods"):
        listed = ", ".join(sorted((set(missing["periods"]) - {None}) | unheld, key=_period_key))
        return _bad(f"the course has no class in period {listed} on {missing['day']}")
    # A new start or end is one meeting's, so the apps and pushes know which.
    if several := broken.get("several_meetings"):
        return _bad(
            f"the course meets more than once on {several['day']};"
            f" a {columns['kind']} change needs the periods of one meeting"
        )
    if split := broken.get("not_one_meeting"):
        listed = ", ".join(columns["periods"])
        return _bad(f"period {listed} is not one meeting of the course on {split['day']}")
    if clash := broken.get("clash"):
        day = clash["day"].isoformat()
        # A room change and an online class clash, so the kind is the other change's.
        kind = await conn.fetchval(
            "SELECT kind FROM course_changes WHERE id = $1", clash["change_id"]
        )
        return JSONResponse(
            status_code=409,
            content={"detail": f"{day} already has a {kind} change", "day": day, "kind": kind},
        )
    return columns


def kept_note_en(fields: dict, note_zh: str | None, note_en: str | None) -> dict:
    """`fields`, a replace item's change, with the English text `note_en` of
    the change it replaces kept while its Chinese text stays `note_zh`; the
    extraction states an English text only for an assessment it names."""
    if fields.get("note_en") is None and fields.get("note_zh") == note_zh:
        return {**fields, "note_en": note_en}
    return fields


def item_payload(item) -> dict:
    """A review item's change in `course_changes` column names, a replace's as
    approving it would write it; the item's row carries the replaced change's
    notes as `replaced_note_zh` and `replaced_note_en`."""
    payload = json.loads(item["payload"])
    if item["action"] != "replace":
        return payload
    return kept_note_en(payload, item["replaced_note_zh"], item["replaced_note_en"])


def item_body(item) -> dict:
    """A review item's change as a request body, as the item states it."""
    payload = json.loads(item["payload"])
    first, last = payload.get("start_date"), payload.get("end_date")
    return {
        "course_no": item["course_no"],
        "kind": payload.get("kind"),
        "mode": payload.get("mode"),
        "day": first,
        "from": first,
        "to": last,
        "excluded_weekdays": payload.get("excluded_weekdays"),
        "periods": payload.get("periods"),
        "room": payload.get("room"),
        "start": payload.get("start_time"),
        "end": payload.get("end_time"),
        "note_zh": payload.get("note_zh"),
        "note_en": payload.get("note_en"),
    }


def is_id(value) -> bool:
    return type(value) is int and 0 < value <= BIGINT_MAX


async def pending_item(conn, item_id) -> asyncpg.Record | JSONResponse:
    """Locks review item `item_id` until the transaction ends. Refuses one
    that is no longer pending, or whose extraction a newer version of the
    announcement or a later extraction of it replaced."""
    if not is_id(item_id):
        return _bad("item_id must be an integer")
    item = await conn.fetchrow(_ITEM, item_id)
    if item is None:
        return _bad("item not found", 404)
    if item["state"] != "pending" or not item["current"]:
        return _bad("the item is no longer pending", 409)
    return item


async def _lock_items(conn, change_id: int) -> None:
    """Locks the review items that wrote, replace or remove the change, before
    the change itself: deleting it updates them, and approving one locks them
    in this order too."""
    await conn.execute(
        "SELECT 1 FROM course_announcement_items WHERE course_change_id = $1"
        " ORDER BY id FOR UPDATE",
        change_id,
    )


async def _approve(conn, item_id: int, change_id: int | None, actor: str) -> None:
    await conn.execute(
        "UPDATE course_announcement_items SET state = 'approved', course_change_id = $2,"
        " decided_by = $3, decided_at = now(), updated_at = now() WHERE id = $1",
        item_id,
        change_id,
        actor,
    )


async def _record_on_items(conn, change_id: int, state: str, actor: str) -> None:
    """Marks the items that wrote change `change_id` `edited` or `removed`."""
    await conn.execute(
        "UPDATE course_announcement_items SET state = $2, decided_by = $3,"
        " decided_at = now(), updated_at = now()"
        " WHERE course_change_id = $1 AND state IN ('applied', 'approved', 'edited')",
        change_id,
        state,
        actor,
    )


async def add_change(
    conn, actor: str, term: str, course_no: str, body: dict, item=None
) -> dict | JSONResponse:
    """Checks and inserts the change `body` describes, approving the add item
    `item`, locked, when it comes from one. Returns its id, term, course and
    columns."""
    parsed = await _check(conn, term, course_no, body)
    if isinstance(parsed, JSONResponse):
        return parsed
    change_id = await conn.fetchval(
        f"INSERT INTO course_changes (term_code, course_no, {', '.join(_FIELDS)},"
        " created_by, updated_by, source_announcement_id) VALUES ($1, $2, "
        + ", ".join(f"${i}" for i in range(3, len(_FIELDS) + 3))
        + f", ${len(_FIELDS) + 3}, ${len(_FIELDS) + 3}, ${len(_FIELDS) + 4}) RETURNING id",
        term,
        course_no,
        *(parsed[f] for f in _FIELDS),
        actor,
        item and item["announcement_id"],
    )
    if item:
        await _approve(conn, item["id"], change_id, actor)
    return {**parsed, "id": change_id, "term_code": term, "course_no": course_no}


async def replace_change(
    conn, actor: str, change_id: int, body: dict, item_id=None, keep_note_en=False
) -> dict | JSONResponse:
    """Replaces change `change_id` with the one `body` describes, approving
    the replace item `item_id` when it comes from one. With `keep_note_en`,
    `body` is the item's own change, which keeps the change's English note as
    `kept_note_en` does. Returns its term, course and columns."""
    await _lock_items(conn, change_id)
    # NO KEY UPDATE, since the id stays: a term delete's foreign key check
    # then still reads this row, and cannot deadlock with the term lock below.
    current = await conn.fetchrow(
        "SELECT term_code, course_no, note_zh, note_en FROM course_changes"
        " WHERE id = $1 FOR NO KEY UPDATE",
        change_id,
    )
    if current is None:
        return _bad("change not found", 404)
    if keep_note_en:
        body = kept_note_en(body, current["note_zh"], current["note_en"])
    item = None
    if item_id is not None:
        item = await pending_item(conn, item_id)
        if isinstance(item, JSONResponse):
            return item
        if item["action"] != "replace" or item["course_change_id"] != change_id:
            return _bad("the item does not replace this change", 409)
    if "course_no" in body and _text(body["course_no"]) != current["course_no"]:
        return _bad(
            f"the change belongs to {current['course_no']}; add one to the other course"
        )
    parsed = await _check(conn, current["term_code"], current["course_no"], body, change_id)
    if isinstance(parsed, JSONResponse):
        return parsed
    await conn.execute(
        "UPDATE course_changes SET "
        + ", ".join(f"{f} = ${i}" for i, f in enumerate(_FIELDS, start=2))
        + f", updated_by = ${len(_FIELDS) + 2}, updated_at = now() WHERE id = $1",
        change_id,
        *(parsed[f] for f in _FIELDS),
        actor,
    )
    await _record_on_items(conn, change_id, "edited", actor)
    if item:
        await _approve(conn, item["id"], change_id, actor)
    return {**parsed, "term_code": current["term_code"], "course_no": current["course_no"]}


async def remove_change(conn, actor: str, change_id: int, item_id=None):
    """Deletes change `change_id`, approving the remove item `item_id` when it
    comes from one and superseding the other pending items that would replace
    or remove it. Returns its term, course and kind."""
    await _lock_items(conn, change_id)
    item = None
    if item_id is not None:
        item = await pending_item(conn, item_id)
        if isinstance(item, JSONResponse):
            return item
        if item["action"] != "remove" or item["course_change_id"] != change_id:
            return _bad("the item does not remove this change", 409)
    # Before the delete clears their link to it.
    await _record_on_items(conn, change_id, "removed", actor)
    await conn.execute(
        "UPDATE course_announcement_items SET state = 'superseded', updated_at = now()"
        " WHERE course_change_id = $1 AND state = 'pending' AND id IS DISTINCT FROM $2::bigint",
        change_id,
        item_id,
    )
    row = await conn.fetchrow(
        "DELETE FROM course_changes WHERE id = $1 RETURNING term_code, course_no, kind",
        change_id,
    )
    if row is None:
        return _bad("change not found", 404)
    if item:
        await _approve(conn, item["id"], None, actor)
    return row


def _item_detail(item_id) -> dict:
    return {} if item_id is None else {"item_id": item_id}


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
                        {"day": m["day"], "periods": sorted(m["periods"], key=_period_key)}
                        for m in json.loads(r["meetings"])
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
            _CHANGES + " AND ($2::text IS NULL OR c.course_no = $2)"
            " ORDER BY c.start_date, c.course_no, c.id",
            term,
            course_no,
        )
    return JSONResponse(content={"term": term, "changes": [_change_to_dict(r) for r in rows]})


@router.post("/terms/{term}/changes")
async def create_change(
    term: str, request: Request, pool=Depends(get_pool), body: dict = Body(...)
) -> JSONResponse:
    """Adds a change; with `item_id`, the add item it approves."""
    course_no = _text(body.get("course_no"))
    if course_no is None:
        return _bad("course_no required")
    item_id = body.get("item_id")
    async with pool.acquire() as conn, conn.transaction():
        item = None
        if item_id is not None:
            item = await pending_item(conn, item_id)
            if isinstance(item, JSONResponse):
                return item
            if (item["action"], item["term_code"], item["course_no"]) != ("add", term, course_no):
                return _bad("the item does not add a change to this course", 409)
        written = await add_change(conn, request.state.actor, term, course_no, body, item)
        if isinstance(written, JSONResponse):
            return written
        change = await conn.fetchrow(_CHANGES + " AND c.id = $2", term, written["id"])
        await audit(
            conn, request, "course_change.create",
            change_id=written["id"], term=term, course_no=course_no, kind=written["kind"],
            **_item_detail(item_id),
        )
    return JSONResponse(content=_change_to_dict(change))


@router.put("/changes/{change_id}")
async def update_change(
    change_id: int, request: Request, pool=Depends(get_pool), body: dict = Body(...)
) -> JSONResponse:
    """Replaces the change; its term and course stay. With `item_id`, the
    replace item it approves."""
    item_id = body.get("item_id")
    async with pool.acquire() as conn, conn.transaction():
        written = await replace_change(conn, request.state.actor, change_id, body, item_id)
        if isinstance(written, JSONResponse):
            return written
        change = await conn.fetchrow(_CHANGES + " AND c.id = $2", written["term_code"], change_id)
        await audit(
            conn, request, "course_change.update",
            change_id=change_id, term=written["term_code"],
            course_no=written["course_no"], kind=written["kind"],
            **_item_detail(item_id),
        )
    return JSONResponse(content=_change_to_dict(change))


@router.delete("/changes/{change_id}")
async def delete_change(
    change_id: int, request: Request, pool=Depends(get_pool)
) -> JSONResponse:
    # Hard delete: the feed sends a term's changes whole, so a change it
    # leaves out is gone; admin_audit_log keeps who deleted what.
    async with pool.acquire() as conn, conn.transaction():
        row = await remove_change(conn, request.state.actor, change_id)
        if isinstance(row, JSONResponse):
            return row
        await audit(
            conn, request, "course_change.delete",
            change_id=change_id, term=row["term_code"],
            course_no=row["course_no"], kind=row["kind"],
        )
    return JSONResponse(content={"deleted": change_id})
