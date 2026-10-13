"""`outside_term`: the days no published term runs on, which the class
reminders and the class Live Activity starts share with both apps'
`AcademicCalendar.isInSession`."""

from __future__ import annotations

from datetime import date

import pytest

from server.academic_calendar.terms import outside_term

FALL = (date(2026, 9, 7), date(2027, 1, 8))
SPRING = (date(2027, 2, 15), date(2027, 6, 18))


def test_with_no_term_published_no_day_is_outside():
    assert outside_term(date(2026, 8, 1), []) is False


@pytest.mark.parametrize(
    ("day", "outside"),
    [
        (date(2026, 9, 6), True),
        (date(2026, 9, 7), False),
        (date(2027, 1, 8), False),
        (date(2027, 1, 9), True),
        (date(2027, 2, 15), False),
        (date(2027, 6, 19), True),
    ],
    ids=[
        "before the first term",
        "its first day",
        "its last day",
        "the winter break",
        "the next term's first day",
        "after the last term",
    ],
)
def test_a_term_runs_from_its_first_day_through_its_last(day, outside):
    assert outside_term(day, [FALL, SPRING]) is outside
