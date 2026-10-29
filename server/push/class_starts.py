"""Class Live Activity starts at send time: the school's course changes
(tigerduck-backend#177) applied to the snapshot the app filed through
`/schedule/sync`, which it may have written from the course's regular times.
"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta, tzinfo

from sqlalchemy.ext.asyncio import AsyncSession

from server.academic_calendar.changes import load_course_changes, meeting_changes
from server.academic_calendar.terms import current_term_code
from server.push.payload import _to_unix_seconds

# The app's `CourseTimeSlot.id`: the course, the day and the meeting's first period.
_SLOT = re.compile(r"(?P<course_no>.+)_(?P<day>[0-9]{8})_(?P<period>[^_]+)")
# The snapshot's subtitle, `HH:mm - HH:mm` in Taipei time in every locale.
_TIMES = re.compile(r"([0-9]{2}:[0-9]{2}) - ([0-9]{2}:[0-9]{2})")


async def recheck_class_start(
    session: AsyncSession, payload: dict, *, tz: tzinfo
) -> tuple[dict | None, timedelta]:
    """The class start's payload as the school's course changes leave its
    meeting, and how much later than filed it is due; None when the meeting
    is cancelled or its changed start (in class: end) has passed.

    A source id that is not a slot id names no course, as for the portal's
    test starts, and goes out as filed. Raises ValueError for a changed start
    or end whose snapshot does not carry the times it replaces.
    """
    slot = _SLOT.fullmatch(str(payload.get("source_id") or ""))
    if slot is None:
        return payload, timedelta(0)
    day = datetime.strptime(slot["day"], "%Y%m%d").date()
    # The term the apps show that day, as the class reminder scan reads it.
    term = await current_term_code(session, datetime.combine(day, time(12), tzinfo=tz))
    if term is None:
        return payload, timedelta(0)
    changed = meeting_changes(
        await load_course_changes(session, [term], course_no=slot["course_no"]),
        term=term,
        course_no=slot["course_no"],
        day=day,
        period=slot["period"],
    )
    if "cancelled" in changed:
        return None, timedelta(0)
    snapshot = dict(payload)
    if "room" in changed:
        snapshot["locationText"] = changed["room"].room
    if "start" not in changed and "end" not in changed:
        return snapshot, timedelta(0)

    times = _TIMES.fullmatch(str(payload.get("subtitle") or ""))
    if times is None:
        raise ValueError("class start without its times")
    start = changed["start"].start_time if "start" in changed else times[1]
    end = changed["end"].end_time if "end" in changed else times[2]
    snapshot["subtitle"] = f"{start} - {end}"
    start_at, end_at = (
        datetime.combine(day, time.fromisoformat(hhmm), tzinfo=tz) for hhmm in (start, end)
    )
    # As the app builds them: classPreparing counts down to the start, inClass
    # runs from the start to the end.
    preparing = payload.get("scenario") == "classPreparing"
    # A meeting moved so that it has begun, or ended while in class, by now
    # needs no countdown to it.
    if (start_at if preparing else end_at) <= datetime.now(tz):
        return None, timedelta(0)
    filed_start = _to_unix_seconds(
        payload.get("countdownTarget" if preparing else "progressStart")
    )
    if preparing:
        snapshot["countdownTarget"] = start_at.isoformat()
    else:
        snapshot["countdownTarget"] = end_at.isoformat()
        snapshot["progressStart"] = start_at.isoformat() if start_at < end_at else None
    if "start" not in changed:
        return snapshot, timedelta(0)
    if filed_start is None:
        raise ValueError("class start without its start time")
    return snapshot, timedelta(seconds=start_at.timestamp() - filed_start)
