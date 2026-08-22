"""Assignment uploads."""

from __future__ import annotations
from datetime import UTC, datetime, timedelta
from fastapi import APIRouter, BackgroundTasks, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.dialects.postgresql import insert as pg_insert
from server.auth.dependencies import CurrentAuthDep
from server.db import SessionDep
from server.sync.models import (
    ChangeEntityType,
    UserAssignment,
    UserAssignmentOverride,
    UserBulletinState,
    UserBulletinSubscription,
    UserChangeLog,
    UserCourse,
    UserCourseOverride,
    UserCourseSkippedDate,
    UserCourseTombstone,
    UserSettingsDocument,
    UserSyncState,
)
from server.syncjobs.log_entries import log_sync
from ._shared import _push_back_sync_jobs

router = APIRouter(prefix="/sync", tags=["sync"])


class AssignmentUploadItem(BaseModel):
    moodle_assignment_id: int
    course_no: str
    course_name: str
    title: str
    due_at: str | None = None
    moodle_url: str | None = None
    is_submitted: bool = False
    grade: str | None = None
class AssignmentUploadRequest(BaseModel):
    assignments: list[AssignmentUploadItem]
@router.post("/assignments/upload")
async def upload_assignments(
    payload: AssignmentUploadRequest,
    auth: CurrentAuthDep,
    session: SessionDep,
):
    """Client-pushed assignment data.  Upserts into ``user_assignments``
    using the actual unique constraint ``(user_id, moodle_course_id,
    moodle_assignment_id)`` — ``moodle_course_id`` is set to ``0`` for
    client-originated rows (the real Moodle course ID is not available on
    the client side).
    """
    now = datetime.now(UTC)
    upserted = 0
    for a in payload.assignments:
        try:
            due_at = datetime.fromisoformat(a.due_at) if a.due_at else None
        except ValueError:
            due_at = None
        stmt = (
            pg_insert(UserAssignment)
            .values(
                user_id=auth.user_id,
                moodle_course_id=0,
                moodle_assignment_id=a.moodle_assignment_id,
                course_no=a.course_no,
                course_name=a.course_name,
                title=a.title,
                due_at=due_at,
                moodle_url=a.moodle_url,
                provider_is_submitted=a.is_submitted,
                provider_grade=a.grade,
                fetched_at=now,
                last_seen_at=now,
            )
            .on_conflict_do_update(
                index_elements=["user_id", "moodle_course_id", "moodle_assignment_id"],
                set_={
                    "course_no": a.course_no,
                    "course_name": a.course_name,
                    "title": a.title,
                    "due_at": due_at,
                    "moodle_url": a.moodle_url,
                    "provider_is_submitted": a.is_submitted,
                    "provider_grade": a.grade,
                    "last_seen_at": now,
                    "updated_at": now,
                },
            )
        )
        await session.execute(stmt)
        upserted += 1
    await log_sync(
        session,
        user_id=auth.user_id,
        source="sync",
        message=f"Assignments uploaded: {upserted} upserted",
        device_id=auth.device_id,
        detail={"upserted": upserted},
    )
    await _push_back_sync_jobs(session, auth.user_id)
    return {"upserted": upserted}
