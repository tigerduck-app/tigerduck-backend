"""The apps' course changes feed, `GET /v3/calendar/course-changes`
(tigerduck-backend#177): a term's changes whole, each with the class days the
server resolved, behind an ETag."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete, update

from server.academic_calendar.models import AcademicHoliday, CourseChange, SemesterTerm
from server.auth.models import User
from server.db import build_session_factory
from server.sync.models import UserCourse

pytestmark = pytest.mark.asyncio(loop_scope="session")

PATH = "/v3/calendar/course-changes"

# Tuesday periods 3-4 and Thursday 2-3.
_DS = [{"day": 2, "periods": ["3", "4"]}, {"day": 4, "periods": [2, 3]}]
# Two meetings on one Monday.
_EL = [{"day": 1, "periods": ["2"]}, {"day": 1, "periods": ["6"]}]


def _factory(client):
    return build_session_factory(client.app.state.engine)


async def _seed(client, *rows) -> None:
    async with _factory(client)() as session:
        session.add_all(rows)
        await session.commit()


async def _courses(client, term: str, *courses: tuple[str, list]) -> None:
    async with _factory(client)() as session:
        user = User()
        session.add(user)
        await session.flush()
        session.add_all(
            UserCourse(
                user_id=user.id,
                semester=term,
                course_key=f"client:{term}:{course_no}",
                course_no=course_no,
                course_name=course_no,
                schedule_json=schedule,
            )
            for course_no, schedule in courses
        )
        await session.commit()


def _change(first: date, last: date | None = None, **fields) -> CourseChange:
    return CourseChange(
        **{
            "term_code": "1151",
            "course_no": "CS2001301",
            "kind": "cancelled",
            "mode": "day" if last is None else "range",
            "start_date": first,
            "end_date": last or first,
            "excluded_weekdays": [],
            "created_by": "ops@example.com",
            "updated_by": "ops@example.com",
            **fields,
        }
    )


async def _term_1151(client) -> None:
    await _seed(
        client,
        SemesterTerm(code="1151", start_date=date(2026, 9, 14), end_date=date(2027, 1, 8)),
        AcademicHoliday(
            name_zh="校慶", name_en="Anniversary",
            start_date=date(2026, 11, 12), end_date=date(2026, 11, 12),
        ),
    )
    await _courses(client, "1151", ("CS2001301", _DS), ("EE2005701", _EL))


async def _feed(client, **params):
    return await client.get(PATH, params=params)


async def test_a_change_is_served_in_the_apps_shape(client) -> None:
    await _term_1151(client)
    await _seed(client, _change(date(2026, 11, 5)))

    response = await _feed(client, term="1151")

    assert response.status_code == 200
    body = response.json()
    assert body["term"] == "1151"
    (change,) = body["changes"]
    updated_at = datetime.fromisoformat(change.pop("updated_at"))
    assert updated_at.utcoffset() == timedelta(hours=8)
    assert isinstance(change.pop("id"), int)
    assert change == {
        "course_no": "CS2001301",
        "kind": "cancelled",
        "mode": "day",
        "day": "2026-11-05",
        "from": None,
        "to": None,
        "excluded_weekdays": [],
        "periods": None,
        "class_days": ["2026-11-05"],
        "room": None,
        "start": None,
        "end": None,
        "note_zh": None,
        "note_en": None,
    }


async def test_each_kind_carries_its_own_value(client) -> None:
    await _term_1151(client)
    await _seed(
        client,
        _change(date(2026, 11, 3), kind="room", room="IB-302", periods=["3", "4"]),
        _change(date(2026, 11, 5), kind="start", start_time="09:40"),
        _change(date(2026, 11, 10), kind="end", end_time="12:00"),
        _change(date(2026, 11, 17), kind="note", note_zh="期中考", note_en="Midterm exam"),
    )

    changes = (await _feed(client, term="1151")).json()["changes"]

    assert [
        (c["kind"], c["room"], c["start"], c["end"], c["note_zh"], c["note_en"])
        for c in changes
    ] == [
        ("room", "IB-302", None, None, None, None),
        ("start", None, "09:40", None, None, None),
        ("end", None, None, "12:00", None, None),
        ("note", None, None, None, "期中考", "Midterm exam"),
    ]
    assert changes[0]["periods"] == ["3", "4"]


async def test_a_range_resolves_its_class_days(client) -> None:
    """Holidays and excluded weekdays come out, so the apps need not."""
    await _term_1151(client)
    await _seed(
        client,
        _change(date(2026, 11, 9), date(2026, 11, 22), kind="room", room="T4-201"),
        _change(
            date(2026, 11, 9), date(2026, 11, 22), course_no="EE2005701",
            excluded_weekdays=[2, 4],
        ),
    )

    changes = (await _feed(client, term="1151")).json()["changes"]
    by_course = {c["course_no"]: c for c in changes}

    room = by_course["CS2001301"]
    assert (room["mode"], room["day"], room["from"], room["to"]) == (
        "range", None, "2026-11-09", "2026-11-22",
    )
    # Thursday 2026-11-12 is the anniversary.
    assert room["class_days"] == ["2026-11-10", "2026-11-17", "2026-11-19"]
    assert by_course["EE2005701"]["excluded_weekdays"] == [2, 4]
    assert by_course["EE2005701"]["class_days"] == ["2026-11-09", "2026-11-16"]


async def test_periods_keep_only_the_days_the_course_meets_in_them(client) -> None:
    await _term_1151(client)
    await _seed(
        client,
        _change(date(2026, 11, 9), date(2026, 11, 20), kind="room", room="T4-201", periods=["3"]),
        _change(date(2026, 11, 9), course_no="EE2005701", periods=["6"]),
    )

    changes = (await _feed(client, term="1151")).json()["changes"]
    by_course = {c["course_no"]: c for c in changes}

    # Thursdays meet in periods 2-3 too; Tuesdays in 3-4.
    assert by_course["CS2001301"]["class_days"] == ["2026-11-10", "2026-11-17", "2026-11-19"]
    assert by_course["EE2005701"]["periods"] == ["6"]
    assert by_course["EE2005701"]["class_days"] == ["2026-11-09"]


async def test_a_change_with_no_class_day_left_is_left_out(client) -> None:
    """A holiday published since, or a course nobody takes any more."""
    await _term_1151(client)
    await _seed(
        client,
        _change(date(2026, 11, 5)),
        _change(date(2026, 11, 12)),
        _change(date(2026, 11, 5), course_no="MA1001703"),
    )

    changes = (await _feed(client, term="1151")).json()["changes"]

    assert [(c["course_no"], c["day"]) for c in changes] == [("CS2001301", "2026-11-05")]


async def test_the_course_schedule_is_its_latest_enrolled_row(client) -> None:
    """The portal's rule: a school row first, then the latest."""
    await _term_1151(client)
    await _courses(client, "1151", ("CS2001301", [{"day": 3, "periods": ["5"]}]))
    async with _factory(client)() as session:
        await session.execute(
            update(UserCourse)
            .where(UserCourse.schedule_json == [{"day": 3, "periods": ["5"]}])
            .values(source="user_added", updated_at=datetime.now(UTC) + timedelta(days=1))
        )
        await session.commit()
    await _seed(client, _change(date(2026, 11, 4)), _change(date(2026, 11, 5)))

    changes = (await _feed(client, term="1151")).json()["changes"]

    assert [c["day"] for c in changes] == ["2026-11-05"]


async def test_only_the_asked_terms_changes_are_served(client) -> None:
    await _term_1151(client)
    await _seed(
        client,
        SemesterTerm(code="1152", start_date=date(2027, 2, 22), end_date=date(2027, 6, 18)),
    )
    await _courses(client, "1152", ("CS2001301", _DS))
    await _seed(client, _change(date(2026, 11, 5)), _change(date(2027, 3, 2), term_code="1152"))

    spring = (await _feed(client, term="1152")).json()

    assert spring["term"] == "1152"
    assert [c["day"] for c in spring["changes"]] == ["2027-03-02"]


async def test_the_term_defaults_to_the_current_one(client) -> None:
    """The apps' current term: the one that began most recently."""
    today = datetime.now(ZoneInfo("Asia/Taipei")).date()
    every_day = [{"day": d, "periods": ["3"]} for d in range(1, 8)]
    await _seed(
        client,
        SemesterTerm(code="1151", start_date=today - timedelta(days=30), end_date=today + timedelta(days=60)),
        SemesterTerm(code="1152", start_date=today + timedelta(days=90), end_date=today + timedelta(days=200)),
    )
    await _courses(client, "1151", ("CS2001301", every_day))
    await _seed(client, _change(today))

    body = (await _feed(client)).json()

    assert body["term"] == "1151"
    assert [c["day"] for c in body["changes"]] == [today.isoformat()]


async def test_an_unknown_term_is_not_found(client) -> None:
    assert (await _feed(client)).status_code == 404
    await _term_1151(client)

    assert (await _feed(client, term="1161")).status_code == 404
    assert (await _feed(client, term="1151")).status_code == 200


async def test_an_unchanged_term_answers_304(client) -> None:
    await _term_1151(client)
    await _seed(client, _change(date(2026, 11, 5)))
    first = await _feed(client, term="1151")
    etag = first.headers["ETag"]

    again = await client.get(PATH, params={"term": "1151"}, headers={"If-None-Match": etag})

    assert again.status_code == 304
    assert again.content == b""
    assert again.headers["ETag"] == etag


async def test_the_etag_is_the_terms_own(client) -> None:
    await _term_1151(client)
    await _seed(
        client,
        SemesterTerm(code="1152", start_date=date(2027, 2, 22), end_date=date(2027, 6, 18)),
    )

    fall = (await _feed(client, term="1151")).headers["ETag"]
    spring = (await _feed(client, term="1152")).headers["ETag"]

    assert fall != spring


async def test_a_deleted_change_changes_the_etag(client) -> None:
    await _term_1151(client)
    await _seed(client, _change(date(2026, 11, 5)), _change(date(2026, 11, 10)))
    before = (await _feed(client, term="1151")).headers["ETag"]

    async with _factory(client)() as session:
        await session.execute(
            delete(CourseChange).where(CourseChange.start_date == date(2026, 11, 10))
        )
        await session.commit()
    after = await client.get(PATH, params={"term": "1151"}, headers={"If-None-Match": before})

    assert after.status_code == 200
    assert after.headers["ETag"] != before
    assert [c["day"] for c in after.json()["changes"]] == ["2026-11-05"]


async def test_an_edit_changes_the_etag(client) -> None:
    await _term_1151(client)
    await _seed(client, _change(date(2026, 11, 3), kind="room", room="IB-302"))
    before = (await _feed(client, term="1151")).headers["ETag"]

    async with _factory(client)() as session:
        await session.execute(update(CourseChange).values(room="IB-303", updated_at=datetime.now(UTC)))
        await session.commit()
    after = await client.get(PATH, params={"term": "1151"}, headers={"If-None-Match": before})

    assert after.status_code == 200
    assert after.json()["changes"][0]["room"] == "IB-303"


async def test_a_holiday_that_moves_class_days_changes_the_etag(client) -> None:
    """The class days are part of what the apps keep, so a calendar edit that
    moves them must not answer 304."""
    await _term_1151(client)
    await _seed(client, _change(date(2026, 11, 9), date(2026, 11, 22), kind="room", room="T4-201"))
    before = (await _feed(client, term="1151")).headers["ETag"]

    await _seed(
        client,
        AcademicHoliday(
            name_zh="停課", name_en="No classes",
            start_date=date(2026, 11, 17), end_date=date(2026, 11, 17),
        ),
    )
    after = await client.get(PATH, params={"term": "1151"}, headers={"If-None-Match": before})

    assert after.status_code == 200
    assert after.json()["changes"][0]["class_days"] == ["2026-11-10", "2026-11-19"]
