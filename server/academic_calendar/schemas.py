"""Wire shapes for the public academic-calendar feed.

Field names are deliberately short and date-only (`YYYY-MM-DD`, no time, no
zone): every consumer is a Taiwan-local calendar day, and handing the clients
an instant would invite each of them to pick its own interpretation of when a
holiday starts.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel


class SemesterOut(BaseModel):
    code: str
    start: date
    #: Inclusive last day of classes.
    end: date
    name_zh: str = ""
    name_en: str = ""


class HolidayOut(BaseModel):
    id: int
    name_zh: str
    name_en: str
    start: date
    #: Inclusive last day; a single-day holiday has start == end.
    end: date


class AcademicCalendarResponse(BaseModel):
    """Everything a client needs to decide "is there class today".

    Sent whole rather than paginated or date-filtered. The entire school
    calendar is a few dozen rows, and a client that holds all of it can
    answer questions about next month offline — which matters because the
    class reminder is scheduled days ahead, from an alarm that fires with no
    network.
    """

    #: Opaque change marker — the newest `updated_at` across both tables,
    #: as an integer epoch second. Clients keep the last one they saw and
    #: skip the parse when it has not moved. Also drives the ETag.
    revision: int
    semesters: list[SemesterOut]
    holidays: list[HolidayOut]
