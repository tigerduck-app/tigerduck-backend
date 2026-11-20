"""What `course_changes` refuses on its own, whoever writes it
(tigerduck-backend#177)."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from server.academic_calendar.models import AcademicHoliday, SemesterTerm
from server.auth.models import User
from server.sync.models import UserCourse

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
        {"kind": "assessment", "note_zh": "期中考", "note_en": "Midterm exam"},
        {"kind": "online"},
        {"kind": "online", "note_zh": "同步視訊", "note_en": "Video call"},
        {"kind": "online", "note_zh": "同步視訊"},
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
        {"kind": "exam", "note_zh": "期中考", "note_en": "Midterm exam"},
        {"kind": "assessment"},
        {"kind": "assessment", "note_zh": "期中考"},
        {"kind": "assessment", "note_zh": "期中考", "note_en": " "},
        {"kind": "assessment", "note_zh": " ", "note_en": "Midterm exam"},
        {"kind": "assessment", "note_en": "Midterm exam"},
        {"kind": "assessment", "note_zh": "期中考", "note_en": "Midterm exam", "room": "IB-101"},
        {"kind": "assessment", "note_zh": "期中考", "note_en": "Midterm exam",
         "start_time": "09:10"},
        {"kind": "online", "room": "IB-101"},
        {"kind": "online", "end_time": "11:00"},
        {"kind": "online", "note_en": "Video call"},
        {"kind": "online", "note_zh": " "},
        {"kind": "cancelled", "note_zh": "停課"},
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


# Monday periods 2 and 6, two meetings; Tuesday 3-4; Friday 10 and A, one meeting
# across the evening's start.
_TAKEN = {"1": ["2", "6"], "2": ["3", "4"], "5": ["10", "A"]}

_VIOLATIONS = text(
    "SELECT code, day, periods, change_id FROM course_change_violations('1151', :course_no,"
    " :kind, :mode, :first, :last, CAST(:excluded AS smallint[]), CAST(:periods AS text[]),"
    " :editing) ORDER BY code, day"
)


async def _taken(db_session) -> None:
    """Term 1151 and one student of CS2001301 at `_TAKEN`."""
    await _term(db_session)
    user = User(student_id="b1")
    db_session.add(user)
    await db_session.flush()
    db_session.add(
        UserCourse(
            user_id=user.id, semester="1151", course_key="client:1151:CS2001301",
            course_no="CS2001301", course_name="資料結構", schedule_json=_TAKEN,
        )
    )
    await db_session.commit()


async def _violations(
    db_session, first, last=None, *, kind="room", periods=None, excluded=(),
    editing=None, course_no="CS2001301",
):
    rows = await db_session.execute(
        _VIOLATIONS,
        {
            "course_no": course_no, "kind": kind,
            "mode": "day" if last is None else "range",
            "first": first, "last": last or first, "excluded": list(excluded),
            "periods": periods, "editing": editing,
        },
    )
    return [
        (code, day and day.isoformat(), missing, change_id)
        for code, day, missing, change_id in rows
    ]


async def _change(db_session, first, last=None, **fields) -> int:
    mode = {"mode": "range", "end_date": last} if last else {"end_date": first}
    change_id = await db_session.scalar(
        _INSERT, {**_ROW, "start_date": first, **mode, **fields}
    )
    await db_session.commit()
    return change_id


async def test_a_range_change_clashes_on_each_day_it_covers(db_session) -> None:
    await _taken(db_session)
    ranged = await _change(
        db_session, date(2026, 11, 16), date(2026, 11, 30), kind="room", room="T4-201",
        excluded_weekdays=[5],
    )

    assert await _violations(db_session, date(2026, 11, 24)) == [
        ("clash", "2026-11-24", None, ranged),
    ]
    assert await _violations(db_session, date(2026, 11, 27)) == []  # its excluded Friday
    assert await _violations(db_session, date(2026, 11, 10), date(2026, 11, 17)) == [
        ("clash", "2026-11-16", None, ranged),
        ("clash", "2026-11-17", None, ranged),
    ]
    assert await _violations(db_session, date(2026, 11, 24), kind="cancelled") == []


async def test_changes_of_a_kind_clash_only_where_their_periods_meet(db_session) -> None:
    await _taken(db_session)
    second = await _change(
        db_session, date(2026, 11, 2), kind="room", room="EE-104", periods=["2"]
    )
    monday = date(2026, 11, 2)

    assert await _violations(db_session, monday, periods=["6"]) == []
    assert await _violations(db_session, monday, periods=["2"]) == [
        ("clash", "2026-11-02", None, second),
    ]
    assert await _violations(db_session, monday) == [("clash", "2026-11-02", None, second)]
    assert await _violations(db_session, monday, kind="note", periods=["2"]) == []


async def test_a_room_change_and_an_online_class_clash(db_session) -> None:
    await _taken(db_session)
    tuesday = date(2026, 11, 3)
    room = await _change(db_session, tuesday, kind="room", room="IB-101")
    online = await _change(db_session, date(2026, 11, 10), kind="online")

    assert await _violations(db_session, tuesday, kind="online") == [
        ("clash", "2026-11-03", None, room),
    ]
    assert await _violations(db_session, date(2026, 11, 10)) == [
        ("clash", "2026-11-10", None, online),
    ]
    assert await _violations(db_session, date(2026, 11, 10), kind="online") == [
        ("clash", "2026-11-10", None, online),
    ]


async def test_an_assessment_clashes_with_assessments_alone(db_session) -> None:
    """An exam may move to another room or start earlier."""
    await _taken(db_session)
    tuesday = date(2026, 11, 3)
    names = {"note_zh": "期中考", "note_en": "Midterm exam"}
    exam = await _change(db_session, tuesday, kind="assessment", **names)
    for fields in (
        {"kind": "room", "room": "IB-101"},
        {"kind": "start", "start_time": "09:00"},
        {"kind": "online"},
        {"kind": "note", "note_zh": "帶計算機"},
    ):
        await _change(db_session, tuesday, **fields)

    assert await _violations(db_session, tuesday, kind="assessment") == [
        ("clash", "2026-11-03", None, exam),
    ]
    for kind in ("cancelled", "end"):
        assert await _violations(db_session, tuesday, kind=kind) == []


@pytest.mark.parametrize(
    ("day", "periods", "expected"),
    [
        (date(2026, 11, 2), None, [("several_meetings", "2026-11-02", None, None)]),
        (date(2026, 11, 2), ["2", "6"], [("not_one_meeting", "2026-11-02", None, None)]),
        (date(2026, 11, 2), ["6"], []),
        (date(2026, 11, 3), ["3"], [("not_one_meeting", "2026-11-03", None, None)]),
        (date(2026, 11, 3), None, []),
        (date(2026, 11, 6), None, []),  # 10 and A are back to back
    ],
)
async def test_a_start_change_names_one_meeting_of_its_day(
    db_session, day, periods, expected
) -> None:
    await _taken(db_session)

    assert await _violations(db_session, day, kind="start", periods=periods) == expected


async def test_periods_must_meet_on_every_day_of_the_change(db_session) -> None:
    await _taken(db_session)

    assert await _violations(db_session, date(2026, 11, 3), periods=["4", "5"]) == [
        ("missing_periods", "2026-11-03", ["5"], None),
    ]
    assert await _violations(
        db_session, date(2026, 11, 2), date(2026, 11, 8), periods=["3"]
    ) == [
        ("missing_periods", "2026-11-02", ["3"], None),
        ("missing_periods", "2026-11-06", ["3"], None),
    ]
    assert await _violations(
        db_session, date(2026, 11, 2), date(2026, 11, 8), periods=["3"], excluded=[1, 5]
    ) == []


async def test_an_edited_change_does_not_clash_with_itself(db_session) -> None:
    await _taken(db_session)
    tuesday = date(2026, 11, 3)
    change = await _change(db_session, tuesday)

    assert await _violations(db_session, tuesday, kind="cancelled", editing=change) == []
    assert await _violations(db_session, tuesday, kind="cancelled") == [
        ("clash", "2026-11-03", None, change),
    ]


async def test_a_change_must_cover_a_class_day(db_session) -> None:
    await _taken(db_session)
    db_session.add(
        AcademicHoliday(
            name_zh="校慶", name_en="Anniversary",
            start_date=date(2026, 11, 10), end_date=date(2026, 11, 10),
        )
    )
    await db_session.commit()
    holiday = date(2026, 11, 10)

    assert await _violations(db_session, date(2026, 11, 4)) == [
        ("no_class_day", "2026-11-04", None, None),
    ]
    assert await _violations(db_session, holiday) == [("holiday", "2026-11-10", None, None)]
    assert await _violations(db_session, holiday, holiday) == [
        ("no_class_day", "2026-11-10", None, None),
    ]


async def test_a_course_nobody_takes_has_no_meetings(db_session) -> None:
    await _taken(db_session)

    assert await _violations(db_session, date(2026, 11, 3), course_no="EE2005701") == [
        ("no_class_day", "2026-11-03", None, None),
        ("no_meetings", None, None, None),
    ]
    # With no change only the course is checked.
    assert await _violations(db_session, None, kind=None, course_no="EE2005701") == [
        ("no_meetings", None, None, None),
    ]
    assert await _violations(db_session, None, kind=None) == []
