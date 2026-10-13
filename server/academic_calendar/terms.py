"""The term the apps operate on, read from the published calendar."""

from __future__ import annotations

from datetime import datetime
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
