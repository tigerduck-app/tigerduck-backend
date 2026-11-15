"""course_change_violations: the rules a course change keeps against the
course's class times and its other changes, and term_courses, each course's
row in a term

Revision ID: bbb33844e87e
Revises: 9e90a76a367e
Create Date: 2026-11-15 18:00:00.000000

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'bbb33844e87e'
down_revision: str | Sequence[str] | None = '9e90a76a367e'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TERM_COURSES = """
CREATE OR REPLACE VIEW term_courses AS
SELECT DISTINCT ON (semester, course_no)
       semester, course_no, course_name, course_name_en, instructors, credits,
       classroom, classroom_map, schedule_json
FROM user_courses
WHERE course_no IS NOT NULL AND enrollment_status = 'enrolled'
ORDER BY semester, course_no, (source = 'ntust_portal') DESC, updated_at DESC
"""

_COURSE_CHANGE_VIOLATIONS = """
CREATE OR REPLACE FUNCTION course_change_violations(
    term text, course text, change_kind text, change_mode text,
    first_day date, last_day date, excluded smallint[], wanted text[],
    editing integer DEFAULT NULL
)
RETURNS TABLE (code text, day date, periods text[], change_id integer)
LANGUAGE sql STABLE
AS $$
    WITH schedule AS (
        SELECT (
            SELECT schedule_json FROM term_courses
            WHERE semester = term AND course_no = course
        ) AS schedule_json
    ), meets AS (
        SELECT m.weekday, m.periods
        FROM schedule CROSS JOIN LATERAL course_meetings(schedule.schedule_json) AS m
    ), days AS (
        SELECT g.moment::date AS day, meets.weekday, meets.periods
        FROM semester_terms AS t
        CROSS JOIN LATERAL generate_series(
            greatest(first_day, t.start_date), least(last_day, t.end_date), interval '1 day'
        ) AS g(moment)
        JOIN meets ON meets.weekday = extract(isodow FROM g.moment)::integer
        WHERE t.code = term
          AND extract(isodow FROM g.moment)::smallint <> ALL (excluded)
    ), ordered AS (
        -- A day's periods in NTUST's order, numbers by value and then letters;
        -- periods next to each other in 1-10, A-D make one meeting, as in the apps.
        SELECT meets.weekday, p.period, row_number() OVER w AS rank,
               coalesce(
                   array_position(o.slots, p.period)
                       <> lag(array_position(o.slots, p.period)) OVER w + 1,
                   true
               ) AS starts_meeting
        FROM meets
        CROSS JOIN LATERAL unnest(meets.periods) AS p(period)
        CROSS JOIN (SELECT '{1,2,3,4,5,6,7,8,9,10,A,B,C,D}'::text[] AS slots) AS o
        WINDOW w AS (
            PARTITION BY meets.weekday
            ORDER BY p.period !~ '^[0-9]+$',
                     CASE WHEN p.period ~ '^[0-9]+$' THEN p.period::numeric END,
                     p.period COLLATE "C"
        )
    ), meetings AS (
        SELECT weekday, array_agg(period) AS periods
        FROM (
            SELECT weekday, period,
                   count(*) FILTER (WHERE starts_meeting)
                       OVER (PARTITION BY weekday ORDER BY rank) AS meeting
            FROM ordered
        ) AS numbered
        GROUP BY weekday, meeting
    )
    SELECT v.code, v.day, v.periods, v.change_id
    FROM (
        SELECT 'no_meetings', NULL::date, NULL::text[], NULL::integer
        WHERE NOT EXISTS (SELECT 1 FROM meets)
        UNION ALL
        SELECT CASE WHEN change_mode = 'day' AND EXISTS (
                   SELECT 1 FROM academic_holidays AS h
                   WHERE first_day BETWEEN h.start_date AND h.end_date
               ) THEN 'holiday' ELSE 'no_class_day' END,
               first_day, NULL, NULL
        FROM schedule
        WHERE course_class_days(
            term, schedule.schedule_json, first_day, last_day, excluded, NULL
        ) = '{}'
        UNION ALL
        SELECT 'missing_periods', d.day,
               ARRAY(SELECT unnest(wanted) EXCEPT SELECT unnest(d.periods)), NULL
        FROM days AS d
        WHERE NOT wanted <@ d.periods
        UNION ALL
        SELECT CASE WHEN wanted IS NULL THEN 'several_meetings' ELSE 'not_one_meeting' END,
               d.day, NULL, NULL
        FROM days AS d
        WHERE change_kind IN ('start', 'end')
          AND CASE WHEN wanted IS NULL
                   THEN (SELECT count(*) FROM meetings AS m WHERE m.weekday = d.weekday) > 1
                   ELSE NOT EXISTS (
                       SELECT 1 FROM meetings AS m
                       WHERE m.weekday = d.weekday
                         AND m.periods @> wanted AND m.periods <@ wanted
                   )
              END
        UNION ALL
        SELECT 'clash', d.day, NULL, c.id
        FROM days AS d
        JOIN course_changes AS c
          ON c.term_code = term AND c.course_no = course AND c.kind = change_kind
         AND c.id IS DISTINCT FROM editing
         AND d.day BETWEEN c.start_date AND c.end_date
         AND extract(isodow FROM d.day)::smallint <> ALL (c.excluded_weekdays)
         AND (c.periods IS NULL OR c.periods <@ d.periods)
         AND (wanted IS NULL OR c.periods IS NULL OR wanted && c.periods)
        WHERE change_kind <> 'note'
    ) AS v(code, day, periods, change_id)
    WHERE change_kind IS NOT NULL OR v.code = 'no_meetings'
$$
"""


def upgrade() -> None:
    """The view `TERM_COURSES_SQL` and the function
    `COURSE_CHANGE_VIOLATIONS_SQL` in server/academic_calendar/models.py
    declare. A second run changes nothing."""
    op.execute(_TERM_COURSES)
    op.execute(_COURSE_CHANGE_VIOLATIONS)


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS course_change_violations("
        "text, text, text, text, date, date, smallint[], text[], integer)"
    )
    op.execute("DROP VIEW IF EXISTS term_courses")
