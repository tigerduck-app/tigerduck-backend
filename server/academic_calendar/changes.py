"""The school's course changes as the apps' feed and the class pushes read
them (tigerduck-backend#177): each change with the class days today's
calendar leaves it.

The portal writes the changes (portal/app/routes/course_changes.py) and reads
them with the same rule; the portal image cannot import `server`, so both
work the class days out in the database, with `course_class_days`.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import date

from sqlalchemy import Row, String, bindparam, exists, select, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from server.academic_calendar.models import CourseChange

# A course's schedule is its latest enrolled row's in the term, a school row
# first, as the portal's `_LATEST` reads it.
_CHANGES = text(
    """
WITH latest AS (
    SELECT DISTINCT ON (semester, course_no) semester, course_no, schedule_json
    FROM user_courses
    WHERE semester = ANY(:terms) AND course_no IS NOT NULL
      AND (CAST(:course_no AS text) IS NULL OR course_no = :course_no)
      AND enrollment_status = 'enrolled'
    ORDER BY semester, course_no, (source = 'ntust_portal') DESC, updated_at DESC
)
SELECT c.*, course_class_days(c.term_code, l.schedule_json, c.start_date,
                              c.end_date, c.excluded_weekdays, c.periods) AS class_days
FROM course_changes c
LEFT JOIN latest l ON l.semester = c.term_code AND l.course_no = c.course_no
WHERE c.term_code = ANY(:terms)
  AND (CAST(:course_no AS text) IS NULL OR c.course_no = :course_no)
ORDER BY c.start_date, c.course_no, c.id
"""
).bindparams(
    bindparam("terms", type_=ARRAY(String)), bindparam("course_no", type_=String)
)


async def load_course_changes(
    session: AsyncSession, terms: Collection[str], *, course_no: str | None = None
) -> Sequence[Row]:
    """The changes of `terms`, by first day; with `course_no`, that course's
    alone. A change whose `class_days` are empty stays: a holiday a user put
    back is a class day for that user."""
    if not terms:
        return []
    if course_no is not None and not await session.scalar(
        # The send-time check asks once per push; most courses have no change,
        # and this index lookup spares them the scan of the term's rows.
        select(
            exists().where(
                CourseChange.term_code.in_(list(terms)),
                CourseChange.course_no == course_no,
            )
        )
    ):
        return []
    return (
        await session.execute(_CHANGES, {"terms": list(terms), "course_no": course_no})
    ).all()


def meeting_changes(
    changes: Sequence[Row], *, term: str, course_no: str, day: date, period: str
) -> dict[str, Row]:
    """The changes, by kind, to the meeting of `course_no` that starts in
    `period` on `day`; notes left out.

    The portal takes one change of a kind per course, day and period, so a
    kind has at most one here. A change covers the days of its own range, not
    `class_days`, so it reaches a holiday the user put back too; a push on any
    other holiday is stopped before it asks.
    """
    return {
        change.kind: change
        for change in changes
        if change.kind != "note"
        and change.term_code == term
        and change.course_no == course_no
        and change.start_date <= day <= change.end_date
        and day.isoweekday() not in change.excluded_weekdays
        and (change.periods is None or period in change.periods)
    }
