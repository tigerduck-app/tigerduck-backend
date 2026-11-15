"""The operator route that queues an announcement's extraction again, which
only the admin listener serves; the portal's Re-extract calls it."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status

from server.academic_calendar.terms import current_term_code
from server.admin_audit import AdminAuditLog
from server.course_announcements.ingest import queue_extractions
from server.course_announcements.models import AnnouncementRoute, CourseAnnouncement
from server.db import SessionDep
from server.security import AdminActor, require_shared_secret

admin_router = APIRouter(
    prefix="/admin/course-announcements",
    tags=["admin"],
    dependencies=[Depends(require_shared_secret)],
)


@admin_router.post("/{announcement_id}/extract")
async def queue_extraction(announcement_id: str, actor: AdminActor, session: SessionDep) -> dict:
    """Queues the stored version of a current-term announcement the route
    rule sends, unless an extraction of it is queued or running."""
    term = await current_term_code(session, datetime.now(UTC))
    if await queue_extractions(session, term, CourseAnnouncement.id == announcement_id):
        session.add(
            AdminAuditLog(
                actor=actor,
                action="course_announcement.reextract",
                detail={"announcement_id": announcement_id},
            )
        )
        return {"queued": True}
    announcement = await session.get(CourseAnnouncement, announcement_id)
    if announcement is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "announcement not found")
    if announcement.route != AnnouncementRoute.sent.value:
        detail = "the announcement is not sent for extraction"
    elif announcement.term_code != term:
        detail = "the announcement is not of the current term"
    else:
        detail = "an extraction of this version is already waiting"
    raise HTTPException(status.HTTP_409_CONFLICT, detail)
