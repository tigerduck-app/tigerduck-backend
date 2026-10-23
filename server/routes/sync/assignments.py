"""Assignment uploads."""

from __future__ import annotations
from datetime import UTC, datetime, timedelta
from fastapi import APIRouter, BackgroundTasks, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from server.auth.dependencies import CurrentAuthDep
from server.db import SessionDep, in_array
from server.push.submission_cancel import cancel_for_submitted
from server.sync.changelog import append_change, lock_sync_state
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
from server.syncjobs.models import SyncJobType
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
    """Client-pushed assignment data, one row per assignment.

    The apps do not know an assignment's Moodle course id. Once the
    server's fetch has the assignment, a row under its real course id, live
    or soft-deleted, the upload only reports a submission and a grade
    there; the fetch owns the rest. Before that the upload keeps a
    placeholder with course id 0, which the fetch takes over. A submission
    is never taken back, since Moodle never un-submits, and a new one
    cancels the assignment's pending reminders as the submission probe does.
    Each row the upload changes goes to the change log as the fetch logs its
    own.
    """
    now = datetime.now(UTC)
    # The fetch takes over a placeholder under this lock; without it, an
    # upload could add a second placeholder beside the row it just took.
    locked_state = await lock_sync_state(session, auth.user_id)
    rows = (
        await session.execute(
            select(UserAssignment)
            .where(
                UserAssignment.user_id == auth.user_id,
                in_array(
                    UserAssignment.moodle_assignment_id,
                    {a.moodle_assignment_id for a in payload.assignments},
                ),
            )
            .order_by(
                UserAssignment.moodle_course_id == 0,
                UserAssignment.deleted_at.is_not(None),
                UserAssignment.id,
            )
        )
    ).scalars()
    by_assignment: dict[int, UserAssignment] = {}
    for row in rows:
        by_assignment.setdefault(row.moodle_assignment_id, row)

    submitted: set[int] = set()
    changed: dict[int, list[str]] = {}
    for a in payload.assignments:
        try:
            due_at = datetime.fromisoformat(a.due_at) if a.due_at else None
        except ValueError:
            due_at = None
        row = by_assignment.get(a.moodle_assignment_id)
        if row is None:
            row = UserAssignment(
                user_id=auth.user_id,
                moodle_course_id=0,
                moodle_assignment_id=a.moodle_assignment_id,
                provider_is_submitted=False,
                fetched_at=now,
            )
            session.add(row)
            by_assignment[a.moodle_assignment_id] = row
        fields = changed.setdefault(a.moodle_assignment_id, [])
        if row.moodle_course_id == 0:
            for name, value in (
                ("course_no", a.course_no),
                ("course_name", a.course_name),
                ("title", a.title),
                ("due_at", due_at),
                ("moodle_url", a.moodle_url),
            ):
                if getattr(row, name) != value:
                    setattr(row, name, value)
                    fields.append(name)
            row.last_seen_at = now
        # No fetch writes a grade; an upload without one has none to tell.
        if a.grade is not None and a.grade != row.provider_grade:
            row.provider_grade = a.grade
            fields.append("provider_grade")
        if a.is_submitted and not row.provider_is_submitted:
            row.provider_is_submitted = True
            row.provider_submitted_at = row.provider_submitted_at or now
            submitted.add(a.moodle_assignment_id)
            fields += ["provider_is_submitted", "provider_submitted_at"]
    await session.flush()
    for moodle_assignment_id, fields in changed.items():
        if fields:
            await append_change(
                session,
                user_id=auth.user_id,
                entity_type=ChangeEntityType.assignment.value,
                entity_id=str(by_assignment[moodle_assignment_id].id),
                operation="upsert",
                payload={"fields": fields},
                device_id=auth.device_id,
                locked_state=locked_state,
            )
    await cancel_for_submitted(
        session, user_id=auth.user_id, moodle_assignment_ids=submitted, now=now
    )
    upserted = len(payload.assignments)
    await log_sync(
        session,
        user_id=auth.user_id,
        source="sync",
        message=f"Assignments uploaded: {upserted} upserted",
        device_id=auth.device_id,
        detail={"upserted": upserted},
    )
    await _push_back_sync_jobs(session, auth.user_id, SyncJobType.moodle_assignments.value)
    return {"upserted": upserted}
