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
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
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
    )


class UserHolidayOverride(Base):
    """A user's decision to keep receiving class reminders on one holiday."""

    __tablename__ = "user_holiday_overrides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
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
    )
