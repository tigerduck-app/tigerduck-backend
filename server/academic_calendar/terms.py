"""The published calendar as the apps read it: the term they take as
current, and the days a term runs on.

A term runs from its first day of classes through its last, both inclusive,
as Taipei school days (`AcademicCalendar` on Apple and Android).
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.academic_calendar.models import SemesterTerm

# The calendar's dates are NTUST school days, which both apps read in Taipei
# time. Fixed rather than `course_reminder_timezone`, so that a bad value
# there cannot stop the assignment reminder scan, which reads the term.
_SCHOOL_TZ = ZoneInfo("Asia/Taipei")

#: A term's first and last day of classes, both inclusive.
TermDates = tuple[date, date]


async def current_term_code(session: AsyncSession, now: datetime) -> str | None:
    """The code of the term both apps take as current at `now`, or None
    while no term is published.

    The apps' `AcademicCalendar.currentTerm` rule: the term that began most
    recently, so a break belongs to the term before it, or the earliest
    term while none has begun.
    """
    today = now.astimezone(_SCHOOL_TZ).date()
    began = await session.scalar(
        select(SemesterTerm.code)
        .where(SemesterTerm.start_date <= today)
        .order_by(SemesterTerm.start_date.desc(), SemesterTerm.code.desc())
        .limit(1)
    )
    if began is not None:
        return began
    return await session.scalar(
        select(SemesterTerm.code)
        .order_by(SemesterTerm.start_date, SemesterTerm.code.desc())
        .limit(1)
    )


async def load_term_dates(session: AsyncSession) -> list[TermDates]:
    """Every published term's first and last day of classes."""
    rows = await session.execute(
        select(SemesterTerm.start_date, SemesterTerm.end_date)
    )
    return [(start, end) for start, end in rows.all()]


def outside_term(day: date, terms: Collection[TermDates]) -> bool:
    """Whether no published term runs on `day`, so no class meets.

    The apps' `AcademicCalendar.isInSession`, negated: before the first
    term, in a break and after the last term. With no term published no day
    is outside, as in the apps, so a calendar nobody filled in silences
    nothing.
    """
    return bool(terms) and not any(start <= day <= end for start, end in terms)
