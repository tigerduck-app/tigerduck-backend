"""Phase 4b: generate course start reminder push_jobs.

`user_courses.schedule_json` is a client-uploaded list of
`{"day": <ISO weekday 1-7>, "periods": [<NTUST period>, ...]}` entries.
The server projects each entry onto concrete dates inside the scan
window using the configured period→start-time table (Asia/Taipei by
default), then mints one push_job per (occurrence, offset) with the
occurrence epoch embedded in the dedupe key — same durability story as
assignment reminders: a schedule change creates NEW keys and the scan
cancels pending jobs whose key is no longer valid.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, tzinfo

import structlog

logger = structlog.get_logger(__name__)

_ISO_WEEKDAYS = range(1, 8)

CHANNEL = "course"


def _entry_start_time(
    entry: object, period_times: dict[str, str]
) -> tuple[int, time] | None:
    """Return (iso_weekday, local_start_time) for one schedule entry, or
    None when the entry is malformed / has no known period."""
    if not isinstance(entry, dict):
        return None
    day = entry.get("day")
    if not isinstance(day, int) or day not in _ISO_WEEKDAYS:
        return None
    starts: list[time] = []
    periods = entry.get("periods")
    if not isinstance(periods, list):
        return None
    for period in periods:
        raw = period_times.get(str(period))
        if raw is None:
            continue
        hour, _, minute = raw.partition(":")
        starts.append(time(int(hour), int(minute)))
    if not starts:
        return None
    return day, min(starts)


def course_occurrences(
    schedule_json: list,
    *,
    window_start: datetime,
    window_end: datetime,
    tz: tzinfo,
    period_times: dict[str, str],
) -> list[datetime]:
    """Concrete UTC class-block start times within (window_start, window_end].

    One occurrence per (entry, matching date); a multi-period entry is one
    teaching block starting at its earliest period. Malformed entries and
    unknown periods are skipped. Result is sorted and de-duplicated.
    """
    slots = [
        slot
        for entry in schedule_json
        if (slot := _entry_start_time(entry, period_times)) is not None
    ]
    if not slots:
        return []

    occurrences: set[datetime] = set()
    local_date = window_start.astimezone(tz).date()
    end_date = window_end.astimezone(tz).date()
    while local_date <= end_date:
        weekday = local_date.isoweekday()
        for day, start in slots:
            if day != weekday:
                continue
            occurrence = datetime.combine(local_date, start, tzinfo=tz)
            if window_start < occurrence <= window_end:
                occurrences.add(occurrence.astimezone(window_start.tzinfo))
        local_date += timedelta(days=1)
    return sorted(occurrences)
