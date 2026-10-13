"""The term both apps take as current, read from the published calendar.

The rule is the apps' `AcademicCalendar.currentTerm`: the term that began
most recently, or the earliest one while none has begun.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from server.academic_calendar.models import SemesterTerm
from server.academic_calendar.terms import current_term_code

pytestmark = pytest.mark.asyncio(loop_scope="session")

FALL = ("1141", date(2025, 9, 8), date(2026, 1, 9))
SPRING = ("1142", date(2026, 2, 16), date(2026, 6, 12))
NEXT_FALL = ("1151", date(2026, 9, 7), date(2026, 12, 25))


async def _publish(session, *terms):
    session.add_all(
        SemesterTerm(code=code, start_date=start, end_date=end)
        for code, start, end in terms
    )
    await session.flush()


def _at(*args):
    return datetime(*args, tzinfo=UTC)


async def test_no_term_while_none_is_published(db_session):
    assert await current_term_code(db_session, _at(2026, 3, 1)) is None


async def test_the_term_in_progress_even_with_the_next_one_published(db_session):
    await _publish(db_session, FALL, SPRING, NEXT_FALL)

    assert await current_term_code(db_session, _at(2026, 5, 1)) == "1142"


async def test_a_break_belongs_to_the_term_before_it(db_session):
    await _publish(db_session, FALL, SPRING, NEXT_FALL)

    assert await current_term_code(db_session, _at(2026, 2, 1)) == "1141"
    assert await current_term_code(db_session, _at(2026, 8, 1)) == "1142"


async def test_the_earliest_term_while_none_has_begun(db_session):
    await _publish(db_session, SPRING, NEXT_FALL)

    assert await current_term_code(db_session, _at(2026, 1, 20)) == "1142"


async def test_a_term_begins_at_midnight_in_taipei(db_session):
    """The calendar's dates are school days in Taipei, eight hours ahead of
    UTC, as the apps read them."""
    await _publish(db_session, FALL, SPRING)

    assert await current_term_code(db_session, _at(2026, 2, 15, 15, 59)) == "1141"
    assert await current_term_code(db_session, _at(2026, 2, 15, 16, 0)) == "1142"
