"""ORM models for the academic calendar.

Tables
------
* `semester_terms` — one row per term the school has published, keyed by the
  same 4-character code the rest of the system already files courses under
  (`user_courses.semester`, e.g. "1151", "115H"). Carries the first and last
  day of classes, which the apps previously hardcoded.
* `academic_holidays` — named date ranges on which classes do not meet.
  Deliberately not scoped to a term: a holiday may sit inside one term,
  straddle two, or fall in the gap between them (寒假, 暑假), and forcing a
  term foreign key would make the last of those inexpressible.
* `user_holiday_overrides` — per-user "notify me anyway" exceptions, one row
  per (user, holiday). Rides the normal cloud-sync changelog so a user's
  devices agree when sync is on; devices with sync off keep the choice
  locally and never create a row here.
* `course_changes` — the school's exceptions to one course's classes in a
  term (`course_no` as in `user_courses`), recorded in the portal: a
  cancelled class, a new room, start or end time, or a note.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    DDL,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from server.db import Base


class SemesterTerm(Base):
    """One published term and the dates classes run between."""

    __tablename__ = "semester_terms"

    # The school's own code, not a surrogate key: every other table that
    # mentions a term (`user_courses.semester`, the Moodle id prefix, the
    # client's cache filenames) already uses it, so a synthetic id would just
    # be a second name for the same thing.
    code: Mapped[str] = mapped_column(String(8), primary_key=True)

    start_date: Mapped[date] = mapped_column(Date)
    # Inclusive: the last day classes meet. Stored inclusive because that is
    # how an operator reads a school calendar; the clients apply their own
    # exclusive-bound arithmetic.
    end_date: Mapped[date] = mapped_column(Date)

    # Optional display override. Empty means the client formats the code
    # itself ("1151" -> "115-1"), which is what it already does everywhere
    # else, so most rows never need these filled in.
    name_zh: Mapped[str] = mapped_column(String(64), default="", server_default="")
    name_en: Mapped[str] = mapped_column(String(64), default="", server_default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint("end_date >= start_date", name="chk_semester_term_range"),
    )


class AcademicHoliday(Base):
    """A named range on which classes do not meet."""

    __tablename__ = "academic_holidays"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Both languages are required rather than one-plus-fallback: a holiday
    # name is the whole content of the calendar row and of any notification
    # suppression the user goes looking for, and "中秋節" shown to an English
    # UI reads as a bug. The app picks by locale, Chinese for zh-*.
    name_zh: Mapped[str] = mapped_column(String(128))
    name_en: Mapped[str] = mapped_column(String(128))

    start_date: Mapped[date] = mapped_column(Date)
    #: Inclusive last day — a single-day holiday has start == end.
    end_date: Mapped[date] = mapped_column(Date)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint("end_date >= start_date", name="chk_holiday_range"),
        Index("ix_academic_holidays_start", "start_date"),
    )


class UserHolidayOverride(Base):
    """A user's decision to keep receiving class reminders and class Live
    Activities on one holiday."""

    __tablename__ = "user_holiday_overrides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    holiday_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("academic_holidays.id", ondelete="CASCADE")
    )

    # Only ever true in practice — the default is "do not notify", so an
    # override row exists precisely to say otherwise. Stored as a column
    # rather than implied by row presence so the client can turn it back off
    # without a delete, which keeps the sync changelog a plain upsert.
    notify: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true"
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("user_id", "holiday_id", name="ux_user_holiday_override"),
        # Named explicitly rather than via `index=True`, which would derive
        # `ix_user_holiday_overrides_user_id` and not match the database.
        Index("ix_user_holiday_overrides_user", "user_id"),
    )


class CourseChange(Base):
    """One exception to a course's classes: on one day, or on every class
    day of a range but the weekdays left out. Keep in step with
    `ea77513de257_course_changes.py`."""

    __tablename__ = "course_changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # No cascade: deleting a term must not drop the changes recorded for it.
    term_code: Mapped[str] = mapped_column(
        String(8), ForeignKey("semester_terms.code")
    )
    course_no: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))
    mode: Mapped[str] = mapped_column(String(8))
    # Both inclusive; a day change has start == end.
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    # ISO weekdays, 1 (Monday) to 7, a range leaves out.
    excluded_weekdays: Mapped[list[int]] = mapped_column(
        ARRAY(SmallInteger), default=list, server_default="{}"
    )
    # NTUST periods ("3", "A"); None is every period of the day.
    periods: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    room: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # "HH:mm", Asia/Taipei.
    start_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    end_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    note_zh: Mapped[str | None] = mapped_column(Text, nullable=True)
    # None falls back to note_zh.
    note_en: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    # The operator's Cloudflare Access email.
    created_by: Mapped[str] = mapped_column(Text)
    updated_by: Mapped[str] = mapped_column(Text)

    # A check passes on NULL, so each blank or format test binds only a value
    # that is there.
    __table_args__ = (
        CheckConstraint(
            "kind IN ('cancelled', 'room', 'start', 'end', 'note')",
            name="chk_course_change_kind",
        ),
        CheckConstraint(
            "(mode = 'day' AND end_date = start_date AND excluded_weekdays = '{}')"
            " OR (mode = 'range' AND end_date >= start_date)",
            name="chk_course_change_mode",
        ),
        CheckConstraint(
            "excluded_weekdays <@ '{1,2,3,4,5,6,7}'::smallint[]",
            name="chk_course_change_weekdays",
        ),
        CheckConstraint(
            "periods IS NULL OR cardinality(periods) > 0",
            name="chk_course_change_periods",
        ),
        CheckConstraint(
            "(kind = 'room') = (room IS NOT NULL) AND btrim(room) <> ''",
            name="chk_course_change_room",
        ),
        CheckConstraint(
            "(kind = 'start') = (start_time IS NOT NULL)"
            " AND start_time ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'",
            name="chk_course_change_start",
        ),
        CheckConstraint(
            "(kind = 'end') = (end_time IS NOT NULL)"
            " AND end_time ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'",
            name="chk_course_change_end",
        ),
        CheckConstraint(
            "(kind = 'note') = (note_zh IS NOT NULL) AND btrim(note_zh) <> ''"
            " AND (note_en IS NULL OR kind = 'note') AND btrim(note_en) <> ''",
            name="chk_course_change_note",
        ),
        Index("ix_course_changes_term_course", "term_code", "course_no"),
    )


# The weekdays a `user_courses.schedule_json` meets on, ISO 1-7, each with its
# NTUST periods as text. The apps upload `{"<weekday>": [periods]}`; the list of
# `{"day", "periods"}` entries the class reminder scan reads is accepted too.
COURSE_MEETINGS_SQL = """
CREATE OR REPLACE FUNCTION course_meetings(schedule jsonb)
RETURNS TABLE (weekday integer, periods text[])
LANGUAGE sql IMMUTABLE
AS $$
    SELECT day::integer, array_agg(DISTINCT period)
    FROM (
        SELECT key, value
        FROM jsonb_each(CASE jsonb_typeof(schedule) WHEN 'object' THEN schedule ELSE '{}' END)
        UNION ALL
        SELECT entry->>'day', entry->'periods'
        FROM jsonb_array_elements(
            CASE jsonb_typeof(schedule) WHEN 'array' THEN schedule ELSE '[]' END
        ) AS e(entry)
        WHERE jsonb_typeof(entry->'day') = 'number'
    ) AS m(day, list)
    CROSS JOIN LATERAL jsonb_array_elements_text(
        CASE jsonb_typeof(list) WHEN 'array' THEN list ELSE '[]' END
    ) AS p(period)
    WHERE day IN ('1', '2', '3', '4', '5', '6', '7')
      AND period IS NOT NULL
    GROUP BY 1
$$
"""

# A change's class days by today's calendar: the days from first to last,
# inside the term, not a holiday or an excluded weekday, on which the course
# meets in every one of `wanted` (any period when NULL). One computation for
# the portal, the apps' feed and the class reminders.
COURSE_CLASS_DAYS_SQL = """
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

# After every table: a sql function's body names tables that must exist.
event.listen(Base.metadata, "after_create", DDL(COURSE_MEETINGS_SQL))
event.listen(Base.metadata, "after_create", DDL(COURSE_CLASS_DAYS_SQL))
