"""Whether a class can meet on a day, by the published term dates.

Both apps count a day as in session when a published term runs on it, from
its first day of classes through its last, both inclusive, as Taipei
calendar days (`AcademicCalendar.isInSession` on Apple and Android). The
class reminders and class Live Activity starts the backend sends follow the
same days, so the server never announces a class the apps show as quiet.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.academic_calendar.models import SemesterTerm

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
