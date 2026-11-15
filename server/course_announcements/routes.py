"""The operator route that queues an announcement's extraction again, which
only the admin listener serves; the portal's Re-extract calls it."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

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

# Without a classifier URL the extraction job does not run.
TURNED_OFF = "course announcements are turned off"


@admin_router.post("/{announcement_id}/extract")
async def queue_extraction(
    announcement_id: str, actor: AdminActor, request: Request, session: SessionDep
) -> dict:
    """Queues the stored version of a classified announcement of any term or
    route, unless an extraction of it is queued or running."""
    if not request.app.state.settings.announcement_classifier_url:
        raise HTTPException(status.HTTP_409_CONFLICT, TURNED_OFF)
    announcement = await session.get(CourseAnnouncement, announcement_id)
    if announcement is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "announcement not found")
    if announcement.route == AnnouncementRoute.unclassified.value:
        raise HTTPException(status.HTTP_409_CONFLICT, "the announcement is not classified yet")
    if not await queue_extractions(
        session,
        announcement.term_code,
        CourseAnnouncement.id == announcement_id,
        any_route=True,
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "an extraction of this version is already waiting"
        )
    session.add(
        AdminAuditLog(
            actor=actor,
            action="course_announcement.reextract",
            detail={"announcement_id": announcement_id},
        )
    )
    return {"queued": True}
