"""The portal's Course changes page (`portal/app/routes/course_changes.py`)
against the tables the backend migrates (tigerduck-backend#177). In the
server suite for the reason test_portal_sync_job_retry.py gives."""

from __future__ import annotations

import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from portal.app.db import _asyncpg_dsn  # noqa: E402
from portal.app.routes.academic_calendar import (  # noqa: E402
    create_holiday,
    delete_term,
    upsert_term,
)
from portal.app.routes.course_changes import (  # noqa: E402
    create_change,
    delete_change,
    list_changes,
    list_courses,
    update_change,
)
from server.academic_calendar.models import AcademicHoliday, SemesterTerm  # noqa: E402
from server.auth.models import User, UserDevice  # noqa: E402
from server.sync.models import UserCourse  # noqa: E402

pytestmark = pytest.mark.asyncio(loop_scope="session")

_REQUEST = SimpleNamespace(state=SimpleNamespace(actor="ops@example.com"))
_OTHER = SimpleNamespace(state=SimpleNamespace(actor="lead@example.com"))


class _Edit(SimpleNamespace):
    """A request to a calendar route, which reads its own body."""

    async def json(self) -> dict:
        return self.body

# Tuesday periods 3-4 and Thursday 2-3, keyed by ISO weekday as the apps upload it;
# periods as ints too, as some uploads send them.
_DS = {"2": ["3", "4"], "4": [2, 3]}
# Two meetings on one Monday, in the list form the course upload also takes.
_EL = [{"day": 1, "periods": ["2"]}, {"day": 1, "periods": ["6"]}]


@pytest_asyncio.fixture(loop_scope="session")
async def pool(test_settings, db_session):
    p = await asyncpg.create_pool(
        _asyncpg_dsn(test_settings.database_url), min_size=1, max_size=2
    )
    yield p
    await p.close()


async def _user(db_session, student_id: str, devices: int = 1) -> User:
    user = User(student_id=student_id)
    db_session.add(user)
    await db_session.flush()
    db_session.add_all(
        UserDevice(user_id=user.id, client_device_id=f"{student_id}-{n}", platform="ios")
        for n in range(devices)
    )
    return user


def _course(user: User, course_no: str | None, **fields) -> UserCourse:
    return UserCourse(
        user_id=user.id,
        semester=fields.pop("semester", "1151"),
        course_key=f"client:1151:{course_no or fields.get('course_name')}",
        course_no=course_no,
        **{"course_name": "資料結構", **fields},
    )


@pytest_asyncio.fixture(loop_scope="session")
async def term(db_session):
    """Term 1151 with a holiday on a Thursday the course meets, two students
    of CS2001301 and one of EE2005701."""
    db_session.add_all(
        [
            SemesterTerm(code="1151", start_date=date(2026, 9, 14), end_date=date(2027, 1, 8)),
            SemesterTerm(code="1152", start_date=date(2027, 2, 22), end_date=date(2027, 6, 18)),
            AcademicHoliday(
                name_zh="國慶日", name_en="National Day",
                start_date=date(2026, 10, 9), end_date=date(2026, 10, 10),
            ),
            AcademicHoliday(
                name_zh="校慶", name_en="Anniversary",
                start_date=date(2026, 11, 12), end_date=date(2026, 11, 12),
            ),
        ]
    )
    first = await _user(db_session, "b1", devices=2)
    second = await _user(db_session, "b2")
    third = await _user(db_session, "b3")
    gone = UserDevice(
        user_id=second.id, client_device_id="b2-gone", platform="android",
        deleted_at=datetime.now(UTC),
    )
    db_session.add_all(
        [
            gone,
            _course(
                first, "CS2001301", course_name_en="Data Structures",
                instructors=["陳教授"], credits=3, classroom="TR-412",
                classroom_map={"2-3": "TR-412"}, schedule_json=_DS,
            ),
            _course(second, "CS2001301", schedule_json=_DS, classroom="TR-412"),
            _course(third, "CS2001301", semester="1152", schedule_json=_DS),
            _course(third, "EE2005701", course_name="電子學", schedule_json=_EL),
            _course(second, "MA1001703", enrollment_status="dropped", schedule_json=_DS),
            _course(first, None, source="user_added", course_name="讀書會", schedule_json=_DS),
        ]
    )
    await db_session.commit()


def _body(response) -> dict:
    return json.loads(response.body)


async def _create(pool, request=_REQUEST, **body):
    response = await create_change(
        "1151",
        request,
        pool=pool,
        body={"course_no": "CS2001301", "kind": "cancelled", "mode": "day", **body},
    )
    return response.status_code, _body(response)


async def _audit(pool) -> list[tuple[str, str, dict]]:
    rows = await pool.fetch(
        "SELECT actor, action, detail FROM admin_audit_log ORDER BY id"
    )
    return [(r["actor"], r["action"], json.loads(r["detail"])) for r in rows]


async def test_the_courses_table_lists_each_course_of_the_term_once(term, pool) -> None:
    await _create(pool, day="2026-11-05")

    body = _body(await list_courses("1151", pool=pool))

    assert [c["course_no"] for c in body["courses"]] == ["CS2001301", "EE2005701"]
    ds, el = body["courses"]
    assert ds == {
        "course_no": "CS2001301",
        "name": "資料結構",
        "name_en": "Data Structures",
        "instructors": ["陳教授"],
        "credits": 3.0,
        "classroom": "TR-412",
        "classroom_map": {"2-3": "TR-412"},
        "meetings": [{"day": 2, "periods": ["3", "4"]}, {"day": 4, "periods": ["2", "3"]}],
        "students": 2,
        "devices": 3,
        "changes": 1,
    }
    assert el["meetings"] == [{"day": 1, "periods": ["2", "6"]}]
    assert (el["students"], el["devices"], el["changes"]) == (1, 1, 0)


async def test_an_unknown_term_is_not_found(term, pool) -> None:
    assert (await list_courses("1161", pool=pool)).status_code == 404
    assert (await list_changes("1161", pool=pool)).status_code == 404
    response = await create_change(
        "1161", _REQUEST, pool=pool,
        body={"course_no": "CS2001301", "kind": "cancelled", "mode": "day", "day": "2026-11-05"},
    )
    assert response.status_code == 404


async def test_a_course_nobody_takes_in_the_term_is_not_found(term, pool) -> None:
    status, _ = await _create(pool, course_no="MA1001703", day="2026-11-05")

    assert status == 404


async def test_a_day_change_is_recorded_with_who_made_it(term, pool) -> None:
    status, change = await _create(
        pool, kind="room", day="2026-11-05", room=" IB-302 ", periods=["3", "2", "3"]
    )

    assert status == 200
    assert isinstance(change["id"], int)
    assert {k: v for k, v in change.items() if k not in ("id", "created_at", "updated_at")} == {
        "class_days": ["2026-11-05"],
        "stale": False,
        "term": "1151",
        "course_no": "CS2001301",
        "kind": "room",
        "mode": "day",
        "day": "2026-11-05",
        "from": None,
        "to": None,
        "excluded_weekdays": [],
        "periods": ["2", "3"],
        "room": "IB-302",
        "start": None,
        "end": None,
        "note_zh": None,
        "note_en": None,
        "created_by": "ops@example.com",
        "updated_by": "ops@example.com",
    }
    assert _body(await list_changes("1151", pool=pool))["changes"] == [change]
    assert await _audit(pool) == [
        (
            "ops@example.com",
            "course_change.create",
            {"change_id": change["id"], "term": "1151", "course_no": "CS2001301", "kind": "room"},
        )
    ]


@pytest.mark.parametrize(
    "day",
    [
        "2026-09-08",  # a Tuesday before the term
        "2027-01-12",  # a Tuesday after it
        "2026-11-12",  # a Thursday holiday
        "2026-11-04",  # a Wednesday, no class
    ],
)
async def test_a_day_must_be_a_class_day_of_the_course(term, pool, day) -> None:
    status, body = await _create(pool, day=day)

    assert status == 400
    assert day in body["detail"]


async def test_a_range_leaves_out_its_excluded_weekdays(term, pool) -> None:
    status, change = await _create(
        pool, kind="room", room="T4-201", mode="range",
        **{"from": "2026-11-16", "to": "2026-12-31"}, excluded_weekdays=[4, 7, 4],
    )
    assert status == 200
    assert (change["day"], change["from"], change["to"]) == (None, "2026-11-16", "2026-12-31")
    assert change["excluded_weekdays"] == [4, 7]

    assert (await _create(pool, kind="room", room="IB-101", day="2026-11-19"))[0] == 200
    status, body = await _create(pool, kind="room", room="IB-101", day="2026-12-01")
    assert status == 409
    assert body["day"] == "2026-12-01"
    assert "2026-12-01" in body["detail"]


@pytest.mark.parametrize(
    "dates",
    [
        {"from": "2026-11-11", "to": "2026-11-12"},  # a Wednesday and a holiday
        {"from": "2026-11-16", "to": "2026-11-30", "excluded_weekdays": [2, 4]},
        {"from": "2026-09-01", "to": "2026-09-30"},  # starts before the term
        {"from": "2026-12-28", "to": "2027-01-15"},  # ends after it
        {"from": "2026-11-20", "to": "2026-11-16"},
    ],
)
async def test_a_range_must_cover_a_class_day_inside_the_term(term, pool, dates) -> None:
    status, _ = await _create(pool, mode="range", **dates)

    assert status == 400


@pytest.mark.parametrize(
    "fields",
    [
        {"day": "2026/11/05"},
        {"day": "20261105"},
        {"day": "2026-W45-4"},
        {"day": None},
        {"kind": "start", "start": "9:10"},
        {"kind": "start", "start": "24:00"},
        {"kind": "start", "start": "09:10:00"},
        {"kind": "start", "start": 910},
        {"kind": "end", "end": "11:60"},
        {"kind": "end"},
        {"mode": "range", "from": "2026-11-16", "to": "2026-12-31", "excluded_weekdays": [0]},
        {"mode": "range", "from": "2026-11-16", "to": "2026-12-31", "excluded_weekdays": [8]},
        {"mode": "range", "from": "2026-11-16", "to": "2026-12-31", "excluded_weekdays": "4"},
        {"mode": "range", "from": "2026-11-16", "to": "2026-12-31", "excluded_weekdays": [True]},
        {"kind": "room"},
        {"kind": "room", "room": "  "},
        {"kind": "note", "note_en": "Quiz"},
        {"kind": "note", "note_zh": " "},
        {"kind": "moved"},
        {"mode": "week"},
        {"course_no": ""},
    ],
)
async def test_a_value_out_of_its_format_is_refused(term, pool, fields) -> None:
    status, _ = await _create(pool, **{"day": "2026-11-05", **fields})

    assert status == 400


async def test_times_and_notes_are_kept_as_given(term, pool) -> None:
    _, start = await _create(pool, kind="start", day="2026-11-05", start="09:40")
    _, end = await _create(pool, kind="end", day="2026-11-05", end="11:40")
    _, note = await _create(pool, kind="note", day="2026-11-05", note_zh=" 小考 ", note_en=" ")
    _, exam = await _create(
        pool, kind="note", day="2026-11-10", note_zh="期中考", note_en="Midterm exam"
    )

    assert (start["start"], start["end"]) == ("09:40", None)
    assert (end["start"], end["end"]) == (None, "11:40")
    assert (note["note_zh"], note["note_en"]) == ("小考", None)
    assert (exam["note_zh"], exam["note_en"]) == ("期中考", "Midterm exam")


async def test_values_of_another_kind_are_dropped(term, pool) -> None:
    """The dialog sends every field whatever the kind."""
    status, change = await _create(
        pool, day="2026-11-05", room="IB-302", start="09:10", end="11:10",
        note_zh="停課", note_en="No class",
    )

    assert status == 200
    assert [change[k] for k in ("room", "start", "end", "note_zh", "note_en")] == [None] * 5


@pytest.mark.parametrize(
    ("fields", "status"),
    [
        ({"day": "2026-11-05", "periods": ["4"]}, 400),  # Thursday meets 2-3
        ({"day": "2026-11-05", "periods": []}, 400),
        ({"day": "2026-11-05", "periods": "3"}, 400),
        ({"day": "2026-11-05", "periods": ["\u00b2"]}, 400),  # a digit int() rejects
        ({"mode": "range", "from": "2026-11-16", "to": "2026-11-30", "periods": ["3"]}, 200),
        ({"mode": "range", "from": "2026-11-16", "to": "2026-11-30", "periods": ["4"]}, 400),
        (
            {
                "mode": "range", "from": "2026-11-16", "to": "2026-11-30",
                "periods": [4], "excluded_weekdays": [4],
            },
            200,
        ),
    ],
)
async def test_periods_are_ones_the_course_meets_on_every_day(
    term, pool, fields, status
) -> None:
    assert (await _create(pool, **fields))[0] == status


async def test_a_change_of_a_kind_clashes_only_where_its_periods_meet(term, pool) -> None:
    def room(**fields):
        return _create(
            pool, course_no="EE2005701", kind="room", room="EE-104", day="2026-11-02", **fields
        )

    assert (await room(periods=["2"]))[0] == 200
    assert (await room(periods=["6"]))[0] == 200
    status, body = await room(periods=["2"])
    assert (status, body["day"]) == (409, "2026-11-02")
    assert (await room())[0] == 409
    assert (await _create(pool, course_no="EE2005701", day="2026-11-02"))[0] == 200
    assert (await _create(pool, course_no="EE2005701", day="2026-11-02", periods=["6"]))[0] == 409


async def test_an_edit_keeps_the_change_and_its_author(term, pool) -> None:
    _, first = await _create(pool, kind="start", day="2026-11-05", start="09:40")
    _, other = await _create(pool, kind="end", day="2026-11-10", end="12:00")

    response = await update_change(
        first["id"], _OTHER, pool=pool,
        body={"course_no": "CS2001301", "kind": "end", "mode": "day", "day": "2026-11-05", "end": "11:40"},
    )

    assert response.status_code == 200
    edited = _body(response)
    assert edited["id"] == first["id"]
    assert (edited["course_no"], edited["kind"], edited["start"], edited["end"]) == (
        "CS2001301", "end", None, "11:40",
    )
    assert (edited["created_by"], edited["updated_by"]) == ("ops@example.com", "lead@example.com")
    assert edited["created_at"] == first["created_at"]

    clash = await update_change(
        first["id"], _OTHER, pool=pool,
        body={"kind": "end", "mode": "day", "day": "2026-11-10", "end": "11:40"},
    )
    assert clash.status_code == 409
    assert _body(clash)["day"] == "2026-11-10"
    unchanged = await update_change(
        other["id"], _OTHER, pool=pool,
        body={"kind": "end", "mode": "day", "day": "2026-11-10", "end": "12:10"},
    )
    assert unchanged.status_code == 200
    invalid = await update_change(
        other["id"], _OTHER, pool=pool,
        body={"kind": "end", "mode": "day", "day": "2026-11-11", "end": "12:10"},
    )
    assert invalid.status_code == 400
    missing = await update_change(
        999999, _OTHER, pool=pool,
        body={"kind": "end", "mode": "day", "day": "2026-11-10", "end": "12:10"},
    )
    assert missing.status_code == 404
    assert [action for _, action, _ in await _audit(pool)] == [
        "course_change.create", "course_change.create",
        "course_change.update", "course_change.update",
    ]


async def test_a_deleted_change_is_gone(term, pool) -> None:
    _, change = await _create(pool, day="2026-11-05")

    response = await delete_change(change["id"], _OTHER, pool=pool)

    assert response.status_code == 200
    assert _body(await list_changes("1151", pool=pool))["changes"] == []
    assert (await delete_change(change["id"], _OTHER, pool=pool)).status_code == 404
    assert (await _audit(pool))[-1] == (
        "lead@example.com",
        "course_change.delete",
        {"change_id": change["id"], "term": "1151", "course_no": "CS2001301", "kind": "cancelled"},
    )


async def test_the_changes_list_is_by_date_and_filters_by_course(term, pool) -> None:
    _, late = await _create(pool, day="2026-12-03")
    _, early = await _create(pool, day="2026-11-05")
    _, other = await _create(pool, course_no="EE2005701", day="2026-11-02")

    every = _body(await list_changes("1151", pool=pool))["changes"]
    one = _body(await list_changes("1151", pool=pool, course_no="CS2001301"))["changes"]

    assert [c["id"] for c in every] == [other["id"], early["id"], late["id"]]
    assert [c["id"] for c in one] == [early["id"], late["id"]]
    assert _body(await list_changes("1152", pool=pool))["changes"] == []



async def test_a_term_with_changes_is_kept(term, pool) -> None:
    await _create(pool, day="2026-11-05")

    response = await delete_term("1151", _REQUEST, pool=pool)

    assert response.status_code == 409
    assert await pool.fetchval("SELECT count(*) FROM semester_terms WHERE code = '1151'") == 1


async def test_an_edit_cannot_move_a_change_to_another_course(term, pool) -> None:
    _, change = await _create(pool, day="2026-11-05")

    response = await update_change(
        change["id"], _OTHER, pool=pool,
        body={"course_no": "EE2005701", "kind": "cancelled", "mode": "day", "day": "2026-11-02"},
    )

    assert response.status_code == 400
    assert "CS2001301" in _body(response)["detail"]
    (stored,) = _body(await list_changes("1151", pool=pool))["changes"]
    assert (stored["course_no"], stored["day"]) == ("CS2001301", "2026-11-05")


async def test_a_change_of_a_course_nobody_takes_any_more_can_only_be_deleted(
    term, pool
) -> None:
    _, change = await _create(pool, day="2026-11-05")
    await pool.execute(
        "UPDATE user_courses SET enrollment_status = 'dropped' WHERE course_no = 'CS2001301'"
    )

    response = await update_change(
        change["id"], _OTHER, pool=pool,
        body={"kind": "cancelled", "mode": "day", "day": "2026-11-10"},
    )

    assert response.status_code == 409
    assert "delete" in _body(response)["detail"]
    assert (await delete_change(change["id"], _OTHER, pool=pool)).status_code == 200


async def test_notes_share_a_day(term, pool) -> None:
    exam = await _create(pool, kind="note", day="2026-11-05", note_zh="期中考")
    makeup = await _create(pool, kind="note", day="2026-11-05", note_zh="補課", periods=["2"])
    week = await _create(
        pool, kind="note", note_zh="線上上課", mode="range",
        **{"from": "2026-11-02", "to": "2026-11-08"},
    )

    assert [status for status, _ in (exam, makeup, week)] == [200, 200, 200]


@pytest.mark.parametrize(
    "schedule",
    ["null", '"Mon 2, 6"', "5", '[null, 5, {"day": 1, "periods": null}, {"day": "1", "periods": [2]}]'],
)
async def test_a_malformed_schedule_counts_as_no_meetings(term, pool, schedule) -> None:
    await pool.execute(
        "UPDATE user_courses SET schedule_json = $1::jsonb, classroom_map = 'null'"
        " WHERE course_no = 'EE2005701'",
        schedule,
    )

    _, el = _body(await list_courses("1151", pool=pool))["courses"]
    status, _ = await _create(pool, course_no="EE2005701", day="2026-11-02")

    assert (el["course_no"], el["meetings"], el["classroom_map"]) == ("EE2005701", [], {})
    assert status == 404


async def test_a_change_lists_the_class_days_a_calendar_edit_leaves_it(term, pool) -> None:
    """Calendar edits stay open; a change shows what is left of it."""
    _, ranged = await _create(
        pool, kind="room", room="T4-201", mode="range",
        **{"from": "2026-11-16", "to": "2026-12-31"}, excluded_weekdays=[4],
    )
    _, single = await _create(pool, day="2026-11-05")
    assert ranged["class_days"] == [
        "2026-11-17", "2026-11-24", "2026-12-01", "2026-12-08",
        "2026-12-15", "2026-12-22", "2026-12-29",
    ]

    async def edit_term(end: str) -> int:
        body = {"start_date": "2026-09-14", "end_date": end}
        return (await upsert_term("1151", _Edit(state=_REQUEST.state, body=body), pool=pool)).status_code

    async def listed() -> dict[int, tuple[list[str], bool]]:
        changes = _body(await list_changes("1151", pool=pool))["changes"]
        return {c["id"]: (c["class_days"], c["stale"]) for c in changes}

    assert await edit_term("2026-11-20") == 200
    holiday = {"name_zh": "停課", "name_en": "No classes", "start_date": "2026-11-05", "end_date": "2026-11-05"}
    response = await create_holiday(_Edit(state=_REQUEST.state, body=holiday), pool=pool)
    assert response.status_code == 200
    assert await listed() == {
        ranged["id"]: (["2026-11-17"], False),
        single["id"]: ([], True),
    }

    assert await edit_term("2026-11-13") == 200
    assert (await listed())[ranged["id"]] == ([], True)


def _range(first: str, last: str, **fields) -> dict:
    return {"kind": "room", "room": "T4-201", "mode": "range", "from": first, "to": last, **fields}


async def test_an_edit_turns_a_day_into_a_range_and_changes_its_weekdays(term, pool) -> None:
    _, change = await _create(pool, kind="room", room="T4-201", day="2026-11-05")

    ranged = _body(
        await update_change(
            change["id"], _OTHER, pool=pool, body=_range("2026-11-16", "2026-11-30")
        )
    )
    tuesdays = _body(
        await update_change(
            change["id"], _OTHER, pool=pool,
            body=_range("2026-11-16", "2026-11-30", excluded_weekdays=[4]),
        )
    )

    assert (ranged["mode"], ranged["day"], ranged["from"], ranged["to"]) == (
        "range", None, "2026-11-16", "2026-11-30",
    )
    assert ranged["class_days"] == [
        "2026-11-17", "2026-11-19", "2026-11-24", "2026-11-26",
    ]
    assert tuesdays["excluded_weekdays"] == [4]
    assert tuesdays["class_days"] == ["2026-11-17", "2026-11-24"]
    (stored,) = _body(await list_changes("1151", pool=pool))["changes"]
    assert stored == tuesdays


async def test_an_edit_into_another_changes_range_clashes(term, pool) -> None:
    _, ranged = await _create(pool, **_range("2026-11-16", "2026-11-30", excluded_weekdays=[4]))
    _, single = await _create(pool, kind="room", room="IB-101", day="2026-11-19")

    onto_a_tuesday = await update_change(
        single["id"], _OTHER, pool=pool,
        body={"kind": "room", "room": "IB-101", "mode": "day", "day": "2026-11-24"},
    )
    widened = await update_change(
        ranged["id"], _OTHER, pool=pool, body=_range("2026-11-16", "2026-11-30")
    )

    assert (onto_a_tuesday.status_code, _body(onto_a_tuesday)["day"]) == (409, "2026-11-24")
    assert (widened.status_code, _body(widened)["day"]) == (409, "2026-11-19")
    assert {c["id"]: c["class_days"] for c in _body(await list_changes("1151", pool=pool))["changes"]} == {
        ranged["id"]: ["2026-11-17", "2026-11-24"],
        single["id"]: ["2026-11-19"],
    }
