"""What `course_changes` refuses on its own, whoever writes it
(tigerduck-backend#177)."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from server.academic_calendar.models import SemesterTerm

pytestmark = pytest.mark.asyncio(loop_scope="session")

_ROW = {
    "term_code": "1151",
    "course_no": "CS2001301",
    "kind": "cancelled",
    "mode": "day",
    "start_date": date(2026, 11, 5),
    "end_date": date(2026, 11, 5),
    "excluded_weekdays": [],
    "periods": None,
    "room": None,
    "start_time": None,
    "end_time": None,
    "note_zh": None,
    "note_en": None,
    "created_by": "ops@example.com",
    "updated_by": "ops@example.com",
}

_INSERT = text(
    "INSERT INTO course_changes (term_code, course_no, kind, mode, start_date,"
    " end_date, excluded_weekdays, periods, room, start_time, end_time, note_zh,"
    " note_en, created_by, updated_by) VALUES (:term_code, :course_no, :kind,"
    " :mode, :start_date, :end_date, :excluded_weekdays, :periods, :room,"
    " :start_time, :end_time, :note_zh, :note_en, :created_by, :updated_by)"
    " RETURNING id"
)


async def _term(db_session) -> None:
    db_session.add(
        SemesterTerm(code="1151", start_date=date(2026, 9, 14), end_date=date(2027, 1, 8))
    )
    await db_session.commit()


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"kind": "room", "room": "IB-302", "periods": ["3", "4"]},
        {"kind": "start", "start_time": "13:40"},
        {"kind": "end", "end_time": "11:40"},
        {"kind": "note", "note_zh": "期中考", "note_en": "Midterm exam"},
        {"kind": "note", "note_zh": "小考"},
        {
            "mode": "range",
            "end_date": date(2027, 1, 4),
            "excluded_weekdays": [2, 7],
        },
    ],
)
async def test_a_change_of_each_kind_is_stored(db_session, fields) -> None:
    await _term(db_session)

    first = await db_session.scalar(_INSERT, {**_ROW, **fields})
    second = await db_session.scalar(_INSERT, {**_ROW, **fields})

    assert second > first


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "moved"},
        {"term_code": "1152"},
        {"mode": "week"},
        {"mode": "day", "end_date": date(2026, 11, 6)},
        {"mode": "day", "excluded_weekdays": [3]},
        {"mode": "range", "end_date": date(2026, 11, 4)},
        {"mode": "range", "end_date": date(2026, 12, 4), "excluded_weekdays": [0]},
        {"mode": "range", "end_date": date(2026, 12, 4), "excluded_weekdays": [8]},
        {"periods": []},
        {"room": "IB-302"},
        {"kind": "room"},
        {"kind": "room", "room": "  "},
        {"kind": "room", "room": "IB-302", "note_zh": "改教室"},
        {"kind": "start"},
        {"kind": "start", "start_time": "9:10"},
        {"kind": "start", "start_time": "24:00"},
        {"kind": "start", "start_time": "09:10:00"},
        {"kind": "start", "start_time": "09:10", "end_time": "11:00"},
        {"kind": "end", "end_time": "11:60"},
        {"kind": "end", "start_time": "11:00"},
        {"kind": "note"},
        {"kind": "note", "note_zh": " "},
        {"kind": "note", "note_en": "Quiz"},
        {"kind": "note", "note_zh": "小考", "note_en": " "},
        {"note_en": "Quiz"},
    ],
)
async def test_a_change_the_rules_rule_out_is_refused(db_session, fields) -> None:
    await _term(db_session)

    with pytest.raises(DBAPIError):
        await db_session.execute(_INSERT, {**_ROW, **fields})


async def test_a_term_with_changes_is_not_deleted(db_session) -> None:
    await _term(db_session)
    await db_session.execute(_INSERT, _ROW)
    await db_session.commit()

    with pytest.raises(IntegrityError):
        await db_session.execute(text("DELETE FROM semester_terms WHERE code = '1151'"))
