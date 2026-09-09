"""Tests for the public academic-calendar feed.

The contract that matters: it answers without a session (every client needs
it signed out), it returns inclusive date-only ranges, and it tells a client
cheaply whether anything changed — including when the change was a delete.
"""

from __future__ import annotations

from datetime import date

import pytest

from server.academic_calendar.models import AcademicHoliday, SemesterTerm
from server.db import build_session_factory

pytestmark = pytest.mark.asyncio(loop_scope="session")

PATH = "/v3/calendar/semesters"


async def seed(client, *, terms=(), holidays=()):
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        for code, start, end in terms:
            session.add(
                SemesterTerm(code=code, start_date=start, end_date=end)
            )
        for name_zh, name_en, start, end in holidays:
            session.add(
                AcademicHoliday(
                    name_zh=name_zh, name_en=name_en, start_date=start, end_date=end
                )
            )
        await session.commit()


async def test_feed_answers_without_a_session(client) -> None:
    """The whole point of the endpoint: a signed-out app, a sync-off app and
    the F-Droid build all have to be able to read it."""
    response = await client.get(PATH)
    assert response.status_code == 200
    body = response.json()
    assert body["semesters"] == []
    assert body["holidays"] == []
    # Distinguishable from "never fetched" on the client side.
    assert body["revision"] == 0


async def test_returns_terms_newest_first_with_inclusive_dates(client) -> None:
    await seed(
        client,
        terms=[
            ("1151", date(2026, 9, 7), date(2026, 12, 25)),
            ("1142", date(2026, 2, 16), date(2026, 6, 12)),
        ],
    )
    body = (await client.get(PATH)).json()
    assert [s["code"] for s in body["semesters"]] == ["1151", "1142"]
    first = body["semesters"][0]
    assert first["start"] == "2026-09-07"
    assert first["end"] == "2026-12-25"


async def test_holiday_carries_both_languages(client) -> None:
    """A holiday name is the entire content of the calendar row, so an
    English UI showing 中秋節 would read as a bug rather than a fallback."""
    await seed(
        client,
        holidays=[("中秋節", "Mid-Autumn Festival", date(2026, 9, 25), date(2026, 9, 27))],
    )
    holiday = (await client.get(PATH)).json()["holidays"][0]
    assert holiday["name_zh"] == "中秋節"
    assert holiday["name_en"] == "Mid-Autumn Festival"
    assert holiday["start"] == "2026-09-25"
    assert holiday["end"] == "2026-09-27"


async def test_single_day_holiday_has_equal_bounds(client) -> None:
    await seed(
        client, holidays=[("元旦", "New Year's Day", date(2027, 1, 1), date(2027, 1, 1))]
    )
    holiday = (await client.get(PATH)).json()["holidays"][0]
    assert holiday["start"] == holiday["end"] == "2027-01-01"


async def test_unchanged_calendar_answers_304(client) -> None:
    await seed(client, terms=[("1151", date(2026, 9, 7), date(2026, 12, 25))])
    first = await client.get(PATH)
    etag = first.headers["ETag"]

    again = await client.get(PATH, headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert again.content == b""


async def test_deleting_a_holiday_changes_the_etag(client) -> None:
    """A delete removes the newest `updated_at` instead of advancing it, so
    a revision-only tag would let a client keep a holiday the operator had
    already taken down."""
    await seed(
        client,
        terms=[("1151", date(2026, 9, 7), date(2026, 12, 25))],
        holidays=[("校慶", "Anniversary", date(2026, 11, 1), date(2026, 11, 1))],
    )
    before = (await client.get(PATH)).headers["ETag"]

    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        holiday = (await session.execute(_all_holidays())).scalars().one()
        await session.delete(holiday)
        await session.commit()

    after = await client.get(PATH, headers={"If-None-Match": before})
    assert after.status_code == 200
    assert after.headers["ETag"] != before
    assert after.json()["holidays"] == []


async def test_an_edit_inside_the_same_second_changes_the_etag(client) -> None:
    """`revision` is whole seconds, and an operator's second edit lands
    within one of the first. A tag built from the revision alone answered
    304 to the second, and the client kept the first edit's dates."""
    await seed(
        client,
        holidays=[("校慶", "Anniversary", date(2026, 11, 1), date(2026, 11, 1))],
    )
    before = (await client.get(PATH)).headers["ETag"]

    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        holiday = (await session.execute(_all_holidays())).scalars().one()
        holiday.name_en = "Founders' Day"
        await session.commit()

    after = await client.get(PATH, headers={"If-None-Match": before})
    assert after.status_code == 200
    assert after.headers["ETag"] != before
    assert after.json()["holidays"][0]["name_en"] == "Founders' Day"


def _all_holidays():
    from sqlalchemy import select

    return select(AcademicHoliday)
