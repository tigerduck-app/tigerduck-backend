"""`outside_term`: the days a term's classes can meet on, which the class
reminders and the class Live Activity starts share with both apps'
`AcademicCalendar.isInSession`."""

from __future__ import annotations

from datetime import date

import pytest

from server.academic_calendar.terms import outside_term

TERMS = {
    "1151": (date(2026, 9, 7), date(2027, 1, 8)),
    "1152": (date(2027, 2, 15), date(2027, 6, 18)),
}


@pytest.mark.parametrize("semester", [None, "1151"])
def test_with_no_term_published_no_day_is_outside(semester):
    assert outside_term(date(2026, 8, 1), {}, semester=semester) is False


@pytest.mark.parametrize(
    ("day", "outside"),
    [
        (date(2026, 9, 6), True),
        (date(2026, 9, 7), False),
        (date(2027, 1, 8), False),
        (date(2027, 1, 9), True),
    ],
    ids=["day before", "first day", "last day", "day after"],
)
def test_a_term_runs_from_its_first_day_through_its_last(day, outside):
    assert outside_term(day, TERMS, semester="1151") is outside


def test_a_courses_own_term_wins_over_the_term_running():
    """The next term's courses, synced in December, while 1151 still runs."""
    assert outside_term(date(2026, 12, 14), TERMS, semester="1152") is True


@pytest.mark.parametrize(
    ("day", "outside"),
    [
        (date(2026, 12, 14), False),
        (date(2027, 2, 1), True),
        (date(2027, 7, 1), True),
    ],
    ids=["inside 1151", "winter break", "after the last term"],
)
@pytest.mark.parametrize("semester", [None, "1161"])
def test_without_dates_of_its_own_any_published_term_will_do(
    day, outside, semester
):
    assert outside_term(day, TERMS, semester=semester) is outside
