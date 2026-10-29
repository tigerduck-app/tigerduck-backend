"""The school's course changes as the apps' feed and the class pushes read
them (tigerduck-backend#177): each change with the class days today's
calendar leaves it.

The portal writes the changes (portal/app/routes/course_changes.py) and reads
them with the same rule; the portal image cannot import `server`, so both
work the class days out in the database, with `course_class_days`.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence

from sqlalchemy import Row, String, bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

# A course's schedule is its latest enrolled row's in the term, a school row
# first, as the portal's `_LATEST` reads it. A change left no class day is
# left out: the apps and the pushes have nothing to apply it to.
_CHANGES = text(
    """
WITH latest AS (
    SELECT DISTINCT ON (semester, course_no) semester, course_no, schedule_json
    FROM user_courses
    WHERE semester = ANY(:terms) AND course_no IS NOT NULL
      AND enrollment_status = 'enrolled'
    ORDER BY semester, course_no, (source = 'ntust_portal') DESC, updated_at DESC
), resolved AS (
    SELECT c.*, course_class_days(c.term_code, l.schedule_json, c.start_date,
                                  c.end_date, c.excluded_weekdays, c.periods) AS class_days
    FROM course_changes c
    LEFT JOIN latest l ON l.semester = c.term_code AND l.course_no = c.course_no
    WHERE c.term_code = ANY(:terms)
)
SELECT * FROM resolved WHERE cardinality(class_days) > 0
ORDER BY start_date, course_no, id
"""
).bindparams(bindparam("terms", type_=ARRAY(String)))


async def load_course_changes(
    session: AsyncSession, terms: Collection[str]
) -> Sequence[Row]:
    """The changes of `terms` that cover a class day, by first day."""
    if not terms:
        return []
    return (await session.execute(_CHANGES, {"terms": list(terms)})).all()

