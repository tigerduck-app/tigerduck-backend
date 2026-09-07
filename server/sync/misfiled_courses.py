"""Client course rows filed under one term but carrying another term's
Moodle id.

Between 2026-08-20 and the SemesterCatalog fix the app attributed the
選課 system's enrolments to the heuristic current term, so 115-1 courses
were uploaded as `client:1142:<no>` with Moodle id `1151<no>`. Upserts
never removed them. The app now ignores such rows on sync; the migration
that imports this statement deletes them server-side so reminders and
the portal stop seeing them. Manual courses and portal rows are safe: the
app always derives their Moodle id from the term it files them under.

The predicate only matches the exact shape the bug produced — a Moodle id
that is a four-character term code followed by the row's own course
number, where that term differs from the one the row is filed under. A
plain numeric Moodle id (e.g. `777`) or anything else never matches.
"""

from sqlalchemy import text

MISFILED_CLIENT_COURSES_DELETE = text(
    "DELETE FROM user_courses "
    "WHERE course_key LIKE 'client:%' "
    "AND length(semester) = 4 "
    "AND moodle_id = left(moodle_id, 4) || split_part(course_key, ':', 3) "
    "AND left(moodle_id, 4) <> semester"
)
