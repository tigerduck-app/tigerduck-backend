"""Coverage-gap detection for the operator's semester-dates page.

Lives in the server suite because the portal has no test runner of its own
and CI only import-checks it — and this is the one piece of portal logic
whose failure is silent. Suppression comes only from holidays, so a
between-term stretch nobody authored a holiday for is a stretch where every
device keeps ringing for last term's timetable.

The function is pure; these tests need no database.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from portal.app.routes.academic_calendar import _uncovered_gaps  # noqa: E402


def term(code: str, start: tuple, end: tuple) -> dict:
    return {"code": code, "start_date": date(*start), "end_date": date(*end)}


def holiday(start: tuple, end: tuple) -> dict:
    return {"start_date": date(*start), "end_date": date(*end)}


def test_winter_break_with_no_holiday_is_reported():
    gaps = _uncovered_gaps(
        [
            term("1151", (2026, 9, 7), (2026, 12, 25)),
            term("1152", (2027, 2, 15), (2027, 6, 11)),
        ],
        [],
    )
    assert len(gaps) == 1
    assert gaps[0]["after"] == "1151"
    assert gaps[0]["before"] == "1152"
    assert gaps[0]["start_date"] == "2026-12-26"
    assert gaps[0]["end_date"] == "2027-02-14"


def test_a_holiday_covering_the_whole_break_clears_it():
    gaps = _uncovered_gaps(
        [
            term("1151", (2026, 9, 7), (2026, 12, 25)),
            term("1152", (2027, 2, 15), (2027, 6, 11)),
        ],
        [holiday((2026, 12, 26), (2027, 2, 14))],
    )
    assert gaps == []


def test_partial_coverage_still_reports_the_remainder():
    """A holiday that covers only part of the break leaves the rest ringing,
    which is exactly the case an operator would assume was handled."""
    gaps = _uncovered_gaps(
        [
            term("1151", (2026, 9, 7), (2026, 12, 25)),
            term("1152", (2027, 2, 15), (2027, 6, 11)),
        ],
        [holiday((2026, 12, 26), (2027, 1, 10))],
    )
    assert len(gaps) == 1
    assert gaps[0]["start_date"] == "2027-01-11"
    assert gaps[0]["end_date"] == "2027-02-14"


def test_back_to_back_terms_have_no_gap():
    gaps = _uncovered_gaps(
        [
            term("1151", (2026, 9, 7), (2026, 12, 25)),
            term("115H", (2026, 12, 26), (2027, 1, 30)),
        ],
        [],
    )
    assert gaps == []


def test_a_single_term_has_nothing_to_span():
    assert _uncovered_gaps([term("1151", (2026, 9, 7), (2026, 12, 25))], []) == []


def test_terms_are_ordered_before_pairing():
    """The endpoint lists terms newest-first for display; gap detection has
    to pair them chronologically or it would compare a term with the one
    before it and find negative gaps."""
    gaps = _uncovered_gaps(
        [
            term("1152", (2027, 2, 15), (2027, 6, 11)),
            term("1151", (2026, 9, 7), (2026, 12, 25)),
        ],
        [],
    )
    assert len(gaps) == 1
    assert gaps[0]["after"] == "1151"
