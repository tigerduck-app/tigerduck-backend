"""The term the apps operate on, read from the published calendar."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.academic_calendar.models import SemesterTerm

# The calendar's dates are NTUST school days, which both apps read in Taipei
# time. Fixed rather than `course_reminder_timezone`, so that a bad value
# there cannot stop the assignment reminder scan, which reads the term.
_SCHOOL_TZ = ZoneInfo("Asia/Taipei")


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


#: A term's first and last day of classes, both inclusive.
TermDates = tuple[date, date]


async def load_term_dates(session: AsyncSession) -> dict[str, TermDates]:
    """Every published term's dates, by its code."""
    rows = await session.execute(
        select(SemesterTerm.code, SemesterTerm.start_date, SemesterTerm.end_date)
    )
    return {code: (start, end) for code, start, end in rows.all()}


def outside_term(
    day: date, terms: Mapping[str, TermDates], *, semester: str | None = None
) -> bool:
    """Whether `day` falls outside the term, so no class of `semester` meets.

    A term with dates runs from its first day of classes through its last,
    and its courses meet only then: the next term's courses, synced before
    it starts, stay quiet while the term before still runs. A term with no
    dates yet, or no `semester`, falls back to the apps' rule, any published
    term running on the day. With no term published no day is outside, as
    in the apps, so a calendar nobody filled in silences nothing.
    """
    if not terms:
        return False
    own = terms.get(semester) if semester is not None else None
    if own is not None:
        start, end = own
        return not (start <= day <= end)
    return not any(start <= day <= end for start, end in terms.values())
