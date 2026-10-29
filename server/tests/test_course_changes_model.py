"""What `course_changes` refuses on its own, whoever writes it
(tigerduck-backend#177)."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from server.academic_calendar.models import AcademicHoliday, SemesterTerm

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


# Tuesday periods 3-4 and Thursday 2-3, the shape the apps upload.
_SCHEDULE = '[{"day": 2, "periods": ["3", "4"]}, {"day": 4, "periods": [2, 3]}]'


async def _class_days(db_session, first, last, excluded=(), periods=None, schedule=_SCHEDULE):
    days = await db_session.scalar(
        text(
            "SELECT course_class_days('1151', CAST(:schedule AS jsonb), :first, :last,"
            " CAST(:excluded AS smallint[]), CAST(:periods AS text[]))"
        ),
        {
            "schedule": schedule, "first": first, "last": last,
            "excluded": list(excluded), "periods": periods,
        },
    )
    return [day.isoformat() for day in days]


async def test_class_days_follow_the_term_holidays_weekdays_and_periods(db_session) -> None:
    """The one computation the portal, the feed and the class reminders share."""
    await _term(db_session)
    db_session.add(
        AcademicHoliday(
            name_zh="校慶", name_en="Anniversary",
            start_date=date(2026, 11, 12), end_date=date(2026, 11, 12),
        )
    )
    await db_session.commit()
    nov = (date(2026, 11, 2), date(2026, 11, 15))

    assert await _class_days(db_session, *nov) == [
        "2026-11-03", "2026-11-05", "2026-11-10",
    ]
    assert await _class_days(db_session, *nov, excluded=[2]) == ["2026-11-05"]
    assert await _class_days(db_session, *nov, periods=["4"]) == ["2026-11-03", "2026-11-10"]
    assert await _class_days(db_session, *nov, periods=["2", "3"]) == ["2026-11-05"]
    assert await _class_days(db_session, date(2027, 1, 1), date(2027, 1, 31)) == [
        "2027-01-05", "2027-01-07",
    ]
    for schedule in ("null", '"Tue 3-4"', "5", '[null, {"day": "2", "periods": [3]}, {"day": 4}]'):
        assert await _class_days(db_session, *nov, schedule=schedule) == []
    assert await _class_days(db_session, *nov, schedule=None) == []


async def test_meetings_read_each_weekday_once(db_session) -> None:
    rows = await db_session.execute(
        text(
            "SELECT weekday, periods FROM course_meetings(CAST(:schedule AS jsonb))"
            " ORDER BY weekday"
        ),
        {"schedule": '[{"day": 1, "periods": ["2"]}, {"day": 1, "periods": [6, "2"]}, {"day": 9, "periods": [1]}]'},
    )

    assert [(weekday, sorted(periods)) for weekday, periods in rows] == [(1, ["2", "6"])]
