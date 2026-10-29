"""course_changes: a course's cancelled classes, room and time changes and
notes per term

Revision ID: ea77513de257
Revises: 8d70b4ee9cc7
Create Date: 2026-10-29 12:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'ea77513de257'
down_revision: str | Sequence[str] | None = '8d70b4ee9cc7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_HHMM = "'^([01][0-9]|2[0-3]):[0-5][0-9]$'"

_COURSE_MEETINGS = """
CREATE OR REPLACE FUNCTION course_meetings(schedule jsonb)
RETURNS TABLE (weekday integer, periods text[])
LANGUAGE sql IMMUTABLE
AS $$
    SELECT (entry->>'day')::integer, array_agg(DISTINCT period)
    FROM jsonb_array_elements(
        CASE jsonb_typeof(schedule) WHEN 'array' THEN schedule ELSE '[]' END
    ) AS e(entry)
    CROSS JOIN LATERAL jsonb_array_elements_text(
        CASE jsonb_typeof(entry->'periods') WHEN 'array' THEN entry->'periods' ELSE '[]' END
    ) AS p(period)
    WHERE jsonb_typeof(entry->'day') = 'number'
      AND entry->>'day' IN ('1', '2', '3', '4', '5', '6', '7')
      AND period IS NOT NULL
    GROUP BY 1
$$
"""

_COURSE_CLASS_DAYS = """
CREATE OR REPLACE FUNCTION course_class_days(
    term text, schedule jsonb, first_day date, last_day date,
    excluded smallint[], wanted text[]
)
RETURNS date[]
LANGUAGE sql STABLE
AS $$
    SELECT coalesce(array_agg(class_day ORDER BY class_day), '{}')
    FROM semester_terms
    CROSS JOIN LATERAL generate_series(
        greatest(first_day, start_date), least(last_day, end_date), interval '1 day'
    ) AS g(moment)
    CROSS JOIN LATERAL (SELECT moment::date AS class_day) AS d
    JOIN course_meetings(schedule) AS m
      ON m.weekday = extract(isodow FROM class_day)::integer
    WHERE code = term
      AND extract(isodow FROM class_day)::smallint <> ALL (excluded)
      AND (wanted IS NULL OR wanted <@ m.periods)
      AND NOT EXISTS (
          SELECT 1 FROM academic_holidays AS h
          WHERE class_day BETWEEN h.start_date AND h.end_date
      )
$$
"""


def upgrade() -> None:
    """The table `CourseChange` in server/academic_calendar/models.py
    declares, and the functions that give a change its class days
    (`COURSE_CLASS_DAYS_SQL` there). A second run, after a rollback that kept
    them, changes nothing."""
    op.create_table(
        "course_changes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "term_code",
            sa.String(length=8),
            sa.ForeignKey("semester_terms.code"),
            nullable=False,
        ),
        sa.Column("course_no", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("mode", sa.String(length=8), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column(
            "excluded_weekdays",
            postgresql.ARRAY(sa.SmallInteger()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("periods", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("room", sa.String(length=128), nullable=True),
        sa.Column("start_time", sa.String(length=5), nullable=True),
        sa.Column("end_time", sa.String(length=5), nullable=True),
        sa.Column("note_zh", sa.Text(), nullable=True),
        sa.Column("note_en", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('cancelled', 'room', 'start', 'end', 'note')",
            name="chk_course_change_kind",
        ),
        sa.CheckConstraint(
            "(mode = 'day' AND end_date = start_date AND excluded_weekdays = '{}')"
            " OR (mode = 'range' AND end_date >= start_date)",
            name="chk_course_change_mode",
        ),
        sa.CheckConstraint(
            "excluded_weekdays <@ '{1,2,3,4,5,6,7}'::smallint[]",
            name="chk_course_change_weekdays",
        ),
        sa.CheckConstraint(
            "periods IS NULL OR cardinality(periods) > 0",
            name="chk_course_change_periods",
        ),
        sa.CheckConstraint(
            "(kind = 'room') = (room IS NOT NULL) AND btrim(room) <> ''",
            name="chk_course_change_room",
        ),
        sa.CheckConstraint(
            f"(kind = 'start') = (start_time IS NOT NULL) AND start_time ~ {_HHMM}",
            name="chk_course_change_start",
        ),
        sa.CheckConstraint(
            f"(kind = 'end') = (end_time IS NOT NULL) AND end_time ~ {_HHMM}",
            name="chk_course_change_end",
        ),
        sa.CheckConstraint(
            "(kind = 'note') = (note_zh IS NOT NULL) AND btrim(note_zh) <> ''"
            " AND (note_en IS NULL OR kind = 'note') AND btrim(note_en) <> ''",
            name="chk_course_change_note",
        ),
        if_not_exists=True,
    )
    op.create_index(
        "ix_course_changes_term_course",
        "course_changes",
        ["term_code", "course_no"],
        if_not_exists=True,
    )
    op.execute(_COURSE_MEETINGS)
    op.execute(_COURSE_CLASS_DAYS)


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS"
        " course_class_days(text, jsonb, date, date, smallint[], text[])"
    )
    op.execute("DROP FUNCTION IF EXISTS course_meetings(jsonb)")
    op.drop_table("course_changes", if_exists=True)
