"""Course reminder generation: occurrence math + scan/cancellation."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from server.push.course_reminders import course_occurrences

pytestmark = pytest.mark.asyncio(loop_scope="session")

TAIPEI = ZoneInfo("Asia/Taipei")
PERIODS = {"1": "08:10", "3": "10:20", "4": "11:20", "A": "18:25"}


def _utc(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=TAIPEI).astimezone(UTC)


class TestCourseOccurrences:
    # 2026-06-10 is a Wednesday (ISO weekday 3).

    def test_weekly_occurrence_inside_window(self):
        occs = course_occurrences(
            [{"day": 3, "periods": [3, 4]}],
            window_start=datetime(2026, 6, 10, 0, 0, tzinfo=UTC),
            window_end=datetime(2026, 6, 12, 0, 0, tzinfo=UTC),
            tz=TAIPEI,
            period_times=PERIODS,
        )
        # Earliest period (3) wins for the block start.
        assert occs == [_utc(2026, 6, 10, 10, 20)]

    def test_multiple_entries_and_weeks(self):
        occs = course_occurrences(
            [{"day": 3, "periods": [1]}, {"day": 5, "periods": ["A"]}],
            window_start=datetime(2026, 6, 10, 0, 0, tzinfo=UTC),
            window_end=datetime(2026, 6, 18, 0, 0, tzinfo=UTC),
            tz=TAIPEI,
            period_times=PERIODS,
        )
        assert occs == [
            _utc(2026, 6, 10, 8, 10),   # Wed this week
            _utc(2026, 6, 12, 18, 25),  # Fri this week
            _utc(2026, 6, 17, 8, 10),   # Wed next week
        ]

    def test_occurrence_outside_window_excluded(self):
        occs = course_occurrences(
            [{"day": 3, "periods": [3]}],
            window_start=datetime(2026, 6, 10, 3, 0, tzinfo=UTC),  # 11:00 TPE
            window_end=datetime(2026, 6, 11, 0, 0, tzinfo=UTC),
            tz=TAIPEI,
            period_times=PERIODS,
        )
        assert occs == []  # 10:20 TPE already before window_start

    def test_unknown_period_and_day_skipped(self):
        occs = course_occurrences(
            [
                {"day": 3, "periods": [99]},     # unknown period
                {"day": 9, "periods": [3]},      # invalid day
                {"day": 3, "periods": []},       # empty periods
                {"periods": [3]},                # missing day
                "garbage",                       # not a dict
            ],
            window_start=datetime(2026, 6, 10, 0, 0, tzinfo=UTC),
            window_end=datetime(2026, 6, 12, 0, 0, tzinfo=UTC),
            tz=TAIPEI,
            period_times=PERIODS,
        )
        assert occs == []

    def test_empty_schedule(self):
        occs = course_occurrences(
            [],
            window_start=datetime(2026, 6, 10, 0, 0, tzinfo=UTC),
            window_end=datetime(2026, 6, 12, 0, 0, tzinfo=UTC),
            tz=TAIPEI,
            period_times=PERIODS,
        )
        assert occs == []
