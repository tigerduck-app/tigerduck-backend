"""course_change_kinds: assessment and online course changes, the clash of a
room change with an online one, and term 1151's exam, presentation and online
notes converted by id

Revision ID: fefd80be557b
Revises: 22370b5daa3b
Create Date: 2026-11-21 12:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'fefd80be557b'
down_revision: str | Sequence[str] | None = '22370b5daa3b'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KIND = "kind IN ('cancelled', 'room', 'start', 'end', 'assessment', 'online', 'note')"
_NOTE = (
    "(kind IN ('assessment', 'online', 'note') OR note_zh IS NULL)"
    " AND (kind NOT IN ('assessment', 'note') OR note_zh IS NOT NULL)"
    " AND (kind <> 'assessment' OR note_en IS NOT NULL)"
    " AND (note_en IS NULL OR note_zh IS NOT NULL)"
    " AND btrim(note_zh) <> '' AND btrim(note_en) <> ''"
)
_OLD_KIND = "kind IN ('cancelled', 'room', 'start', 'end', 'note')"
_OLD_NOTE = (
    "(kind = 'note') = (note_zh IS NOT NULL) AND btrim(note_zh) <> ''"
    " AND (note_en IS NULL OR kind = 'note') AND btrim(note_en) <> ''"
)

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
          ON c.term_code = term AND c.course_no = course
         AND (c.kind = change_kind
              OR (c.kind IN ('room', 'online') AND change_kind IN ('room', 'online')))
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

_CLASH = """c.course_no = course
         AND (c.kind = change_kind
              OR (c.kind IN ('room', 'online') AND change_kind IN ('room', 'online')))
"""
_OLD_COURSE_CHANGE_VIOLATIONS = _COURSE_CHANGE_VIOLATIONS.replace(
    _CLASH, "c.course_no = course AND c.kind = change_kind\n"
)

# Term 1151's notes that are assessments, by id: the note's texts, which a row
# must still hold to be converted, and the assessment's names.
_ASSESSMENTS = (
    *((change_id, "期中考", "Midterm exam", "期中考", "Midterm exam")
      for change_id in (14, 17, 18, 19, 20, 21, 33)),
    (23, "期末考", "Final exam", "期末考", "Final exam"),
    (16, "期中分組簡報", None, "期中分組簡報", "Midterm group presentation"),
    (54, "期末報告", "Final Presentation", "期末簡報", "Final presentation"),
)
# Term 1151's notes that move a whole class online; their text stays as the description.
_ONLINE = (35, 36)
_TERM = "1151"

_TO_ASSESSMENT = sa.text(
    "UPDATE course_changes SET kind = 'assessment', note_zh = :name_zh,"
    " note_en = :name_en, updated_at = now()"
    " WHERE id = :id AND term_code = :term AND kind = 'note' AND note_zh = :note_zh"
)
_TO_ONLINE = sa.text(
    "UPDATE course_changes SET kind = 'online', updated_at = now()"
    " WHERE id = :id AND term_code = :term AND kind = 'note'"
)
_BACK_TO_NOTE = sa.text(
    "UPDATE course_changes SET kind = 'note', note_zh = :note_zh, note_en = :note_en,"
    " updated_at = now()"
    " WHERE id = :id AND term_code = :term AND kind = 'assessment'"
    " AND note_zh = :name_zh AND note_en = :name_en"
)


def _constraints(kind: str, note: str) -> None:
    op.execute(
        "ALTER TABLE course_changes"
        " DROP CONSTRAINT IF EXISTS chk_course_change_kind,"
        " DROP CONSTRAINT IF EXISTS chk_course_change_note,"
        f" ADD CONSTRAINT chk_course_change_kind CHECK ({kind}),"
        f" ADD CONSTRAINT chk_course_change_note CHECK ({note})"
    )


def upgrade() -> None:
    """The constraints and `COURSE_CHANGE_VIOLATIONS_SQL` that
    server/academic_calendar/models.py declares, then the notes of
    `_ASSESSMENTS` and `_ONLINE` converted. A second run changes nothing."""
    _constraints(_KIND, _NOTE)
    op.execute(_COURSE_CHANGE_VIOLATIONS)
    bind = op.get_bind()
    for change_id, note_zh, _note_en, name_zh, name_en in _ASSESSMENTS:
        bind.execute(
            _TO_ASSESSMENT,
            {"id": change_id, "term": _TERM, "note_zh": note_zh,
             "name_zh": name_zh, "name_en": name_en},
        )
    for change_id in _ONLINE:
        bind.execute(_TO_ONLINE, {"id": change_id, "term": _TERM})


def downgrade() -> None:
    """Every assessment and online change becomes the note the feed sends an
    app that does not read its kind (server/academic_calendar/changes.py), the
    converted ones with their texts from before, so the old constraints hold."""
    bind = op.get_bind()
    for change_id, note_zh, note_en, name_zh, name_en in _ASSESSMENTS:
        bind.execute(
            _BACK_TO_NOTE,
            {"id": change_id, "term": _TERM, "note_zh": note_zh, "note_en": note_en,
             "name_zh": name_zh, "name_en": name_en},
        )
    op.execute(
        "UPDATE course_changes SET kind = 'note', updated_at = now() WHERE kind = 'assessment'"
    )
    op.execute(
        "UPDATE course_changes SET kind = 'note', note_zh = coalesce(note_zh, '線上'),"
        " note_en = CASE WHEN note_zh IS NULL THEN 'Online' ELSE note_en END,"
        " updated_at = now() WHERE kind = 'online'"
    )
    _constraints(_OLD_KIND, _OLD_NOTE)
    op.execute(_OLD_COURSE_CHANGE_VIOLATIONS)
