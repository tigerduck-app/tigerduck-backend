"""Public academic-calendar feed.

GET-only and unauthenticated, like `/v3/bulletins`. The data is the school's
own calendar — no user in it, nothing to scope — and every client needs it
regardless of sign-in state: an app that has never been signed into still
shows a class table from cache, and still has to know not to ring on a
public holiday. Requiring a session would leave signed-out users, sync-off
users and the whole F-Droid flavour with no way to get it.

Replaces the `AppConstants.CurrentTerm` constant both apps hardcoded, which
needed a store release every term and could not express a holiday at all.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import structlog
from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import select

from server.academic_calendar.changes import load_course_changes
from server.academic_calendar.models import AcademicHoliday, SemesterTerm
from server.academic_calendar.schemas import (
    AcademicCalendarResponse,
    CourseChangeOut,
    CourseChangesResponse,
    HolidayOut,
    SemesterOut,
)
from server.academic_calendar.terms import current_term_code
from server.db import SessionDep

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/calendar", tags=["academic-calendar"])

_TAIPEI = ZoneInfo("Asia/Taipei")
# As the semesters feed: an operator edit reaches devices the same day, and a
# launch-time fetch on every device is no thundering herd.
_CACHE_CONTROL = "public, max-age=300"


@router.get("/semesters", response_model=AcademicCalendarResponse)
async def get_academic_calendar(
    request: Request, response: Response, session: SessionDep
) -> AcademicCalendarResponse | Response:
    """Every published term and holiday, newest term first.

    The app calls this on every launch to notice edits, so the response
    carries an ETag: an unchanged calendar costs a 304 and no body. The
    revision is the newest `updated_at` across both tables — a single
    integer the client can compare without parsing the payload.
    """
    terms = (
        (await session.execute(select(SemesterTerm).order_by(SemesterTerm.code.desc())))
        .scalars()
        .all()
    )
    holidays = (
        (
            await session.execute(
                select(AcademicHoliday).order_by(AcademicHoliday.start_date)
            )
        )
        .scalars()
        .all()
    )

    stamps = [t.updated_at for t in terms] + [h.updated_at for h in holidays]
    # An empty calendar still needs a stable revision; 0 says "nothing
    # published yet" and lets the client tell that apart from a fetch it
    # has not made.
    revision = int(max(stamps).timestamp()) if stamps else 0

    # The tag hashes the newest timestamp at full precision, not the
    # whole-second `revision`: two edits inside one second would otherwise
    # share a tag, and a conditional client would keep the first edit's
    # dates until something else changed. Row counts ride along so a
    # delete — which removes the newest timestamp rather than advancing it
    # — still changes the tag; revision alone would let a client keep a
    # stale holiday after the operator removed it.
    newest = max(stamps).isoformat() if stamps else "0"
    tag = hashlib.sha256(
        f"{newest}:{len(terms)}:{len(holidays)}".encode()
    ).hexdigest()[:32]
    etag = f'W/"{tag}"'
    cache_control = _CACHE_CONTROL

    if request.headers.get("if-none-match") == etag:
        return Response(
            status_code=status.HTTP_304_NOT_MODIFIED,
            headers={"ETag": etag, "Cache-Control": cache_control},
        )

    payload = AcademicCalendarResponse(
        revision=revision,
        semesters=[
            SemesterOut(
                code=t.code,
                start=t.start_date,
                end=t.end_date,
                name_zh=t.name_zh,
                name_en=t.name_en,
            )
            for t in terms
        ],
        holidays=[
            HolidayOut(
                id=h.id,
                name_zh=h.name_zh,
                name_en=h.name_en,
                start=h.start_date,
                end=h.end_date,
            )
            for h in holidays
        ],
    )

    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = cache_control
    return payload


@router.get("/course-changes", response_model=CourseChangesResponse)
async def get_course_changes(
    request: Request, response: Response, session: SessionDep, term: str | None = None
) -> CourseChangesResponse | Response:
    """A term's course changes, the current term's (`current_term_code`)
    when `term` is left out.

    Always the whole term, so a client replaces its copy of the term on each
    200. The ETag hashes each change's id, edit time and class days: a
    delete, an edit and a calendar edit that moves a change's class days
    each change it.
    """
    code = term or await current_term_code(session, datetime.now(UTC))
    if code is None or await session.get(SemesterTerm, code) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="term_not_found")
    rows = await load_course_changes(session, [code])

    digest = hashlib.sha256(code.encode())
    for row in rows:
        days = ",".join(day.isoformat() for day in row.class_days)
        digest.update(f"|{row.id}:{row.updated_at.isoformat()}:{days}".encode())
    etag = f'W/"{digest.hexdigest()[:32]}"'
    if request.headers.get("if-none-match") == etag:
        return Response(
            status_code=status.HTTP_304_NOT_MODIFIED,
            headers={"ETag": etag, "Cache-Control": _CACHE_CONTROL},
        )

    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = _CACHE_CONTROL
    return CourseChangesResponse(
        term=code,
        changes=[
            CourseChangeOut(
                id=row.id,
                course_no=row.course_no,
                kind=row.kind,
                mode=row.mode,
                day=row.start_date if row.mode == "day" else None,
                from_=row.start_date if row.mode == "range" else None,
                to=row.end_date if row.mode == "range" else None,
                excluded_weekdays=row.excluded_weekdays,
                periods=row.periods,
                class_days=row.class_days,
                room=row.room,
                start=row.start_time,
                end=row.end_time,
                note_zh=row.note_zh,
                note_en=row.note_en,
                updated_at=row.updated_at.astimezone(_TAIPEI).replace(microsecond=0),
            )
            for row in rows
        ],
    )
