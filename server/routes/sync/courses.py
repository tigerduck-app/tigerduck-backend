"""Course deletion. Both endpoints write tombstones rather than removing
rows, so other devices learn about the deletion on their next sync."""

from __future__ import annotations
from datetime import UTC, datetime, timedelta
from fastapi import APIRouter, BackgroundTasks, Query, Request
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from server.auth.dependencies import CurrentAuthDep
from server.db import SessionDep
from server.auth.models import PushDelivery, PushDeliveryStatus, PushJob, PushJobStatus, User, UserDevice
from server.sync.changelog import RevisionExpired, append_change, lock_sync_state, read_changes
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
from ._shared import _trigger_push_tick, logger

router = APIRouter(prefix="/sync", tags=["sync"])


@router.delete("/courses")
async def delete_all_courses(
    auth: CurrentAuthDep,
    session: SessionDep,
    request: Request,
    background_tasks: BackgroundTasks,
    semester: str | None = Query(default=None, max_length=16),
):
    """Wipe the user's courses — every semester, or only `semester` when
    given. Used by 'reset course timetable'."""
    now = datetime.now(UTC)

    course_scope = [UserCourse.user_id == auth.user_id]
    tombstone_scope = [UserCourseTombstone.user_id == auth.user_id]
    if semester:
        course_scope.append(UserCourse.semester == semester)
        tombstone_scope.append(UserCourseTombstone.semester == semester)

    rows = (await session.execute(
        delete(UserCourse)
        .where(*course_scope)
        .returning(UserCourse.id, UserCourse.course_key, UserCourse.semester, UserCourse.course_no)
    )).all()

    # Replace the tombstones in scope rather than merely clearing them.
    #
    # Clearing was enough to unblock this device's immediate re-upload, and
    # it still is -- a reset tombstone does not bind its own author. What it
    # was not enough for is every *other* device: with nothing on record, a
    # phone that had not yet reconciled would push its pre-reset roster back
    # on its next refresh, `upload_courses` had no reason to refuse it, and
    # the term the user had just reset filled straight back up.
    #
    # The clear runs first and unconditionally so the rewrite also covers a
    # user who deleted every course one by one and then reset: those older
    # single-delete tombstones would otherwise keep binding this device.
    #
    # Only a semester reset writes them. A full reset already propagates
    # through `courses_reset_at` below, which tells every device to drop its
    # course overlay wholesale; tombstoning on top of that would put a
    # permanent marker on every course of every past term, and no refresh
    # re-uploads a term the student finished years ago -- their timetables
    # would stay blank for good, on the resetting device too.
    await session.execute(delete(UserCourseTombstone).where(*tombstone_scope))
    if rows and semester:
        await session.execute(
            pg_insert(UserCourseTombstone).values([
                {
                    "user_id": auth.user_id,
                    "course_key": row.course_key,
                    "semester": row.semester,
                    "course_no": row.course_no,
                    "deleted_at": now,
                    "deleted_by_device_id": auth.device_id,
                    "deleted_by_reset": True,
                }
                for row in rows
            ])
        )

    # courses_reset_at tells other devices to wipe their local course
    # overlays wholesale, so only a full reset may set it. A semester reset
    # propagates through the change log + per-semester reconcile instead.
    if semester is None:
        await session.execute(
            update(User).where(User.id == auth.user_id).values(courses_reset_at=now)
        )

    if not rows:
        return {"deleted": 0}

    state = await lock_sync_state(session, auth.user_id)
    for row in rows:
        await append_change(
            session,
            user_id=auth.user_id,
            entity_type=ChangeEntityType.course.value,
            entity_id=str(row.id),
            operation="delete",
            payload={"course_key": row.course_key},
            device_id=auth.device_id,
            locked_state=state,
        )
    dedupe = f"sync_trigger:{auth.user_id}:{int(now.timestamp()) // 300}"
    push_result = await session.execute(
        pg_insert(PushJob)
        .values(
            user_id=auth.user_id,
            dedupe_key=dedupe,
            channel="system",
            scenario="sync_trigger",
            fire_at=now,
            payload={
                "kind": "sync_trigger",
                "source_device_id": str(auth.device_id) if auth.device_id else None,
            },
        )
        .on_conflict_do_nothing()
        .returning(PushJob.id)
    )
    push_row = push_result.scalar_one_or_none()
    if push_row:
        background_tasks.add_task(_trigger_push_tick, request)

    deleted_keys = [r.course_key for r in rows]
    push_status = f"push job #{push_row} queued" if push_row else f"push deduplicated ({dedupe})"
    await log_sync(
        session,
        user_id=auth.user_id,
        source="sync",
        message=f"{'Semester ' + semester if semester else 'All'} courses deleted: {len(rows)} removed — {push_status}",
        device_id=auth.device_id,
        detail={"deleted": len(rows), "semester": semester, "course_keys": deleted_keys},
    )
    logger.info(
        "sync.delete_all_courses",
        user_id=str(auth.user_id),
        semester=semester,
        deleted=len(rows),
    )
    return {"deleted": len(rows)}
@router.delete("/courses/{course_key:path}")
async def delete_course(
    course_key: str,
    auth: CurrentAuthDep,
    session: SessionDep,
    request: Request,
    background_tasks: BackgroundTasks,
):
    now = datetime.now(UTC)
    del_stmt = (
        delete(UserCourse)
        .where(
            UserCourse.user_id == auth.user_id,
            UserCourse.course_key == course_key,
        )
        .returning(UserCourse.id, UserCourse.semester, UserCourse.course_no)
    )
    result = await session.execute(del_stmt)
    deleted_row = result.one_or_none()
    if deleted_row is None:
        return {"deleted": 0}

    await session.execute(
        pg_insert(UserCourseTombstone)
        .values(
            user_id=auth.user_id,
            course_key=course_key,
            semester=deleted_row.semester,
            course_no=deleted_row.course_no,
            deleted_at=now,
            deleted_by_device_id=auth.device_id,
        )
        .on_conflict_do_update(
            index_elements=["user_id", "course_key"],
            set_={"deleted_at": now, "deleted_by_device_id": auth.device_id},
        )
    )

    state = await lock_sync_state(session, auth.user_id)
    await append_change(
        session,
        user_id=auth.user_id,
        entity_type=ChangeEntityType.course.value,
        entity_id=str(deleted_row.id),
        operation="delete",
        payload={"course_key": course_key},
        device_id=auth.device_id,
        locked_state=state,
    )
    dedupe = f"sync_trigger:{auth.user_id}:{int(now.timestamp()) // 300}"
    push_result = await session.execute(
        pg_insert(PushJob)
        .values(
            user_id=auth.user_id,
            dedupe_key=dedupe,
            channel="system",
            scenario="sync_trigger",
            fire_at=now,
            payload={
                "kind": "sync_trigger",
                "source_device_id": str(auth.device_id) if auth.device_id else None,
            },
        )
        .on_conflict_do_nothing()
        .returning(PushJob.id)
    )
    push_row = push_result.scalar_one_or_none()
    if push_row:
        background_tasks.add_task(_trigger_push_tick, request)
    push_status = f"push job #{push_row} queued" if push_row else f"push deduplicated ({dedupe})"
    await log_sync(
        session,
        user_id=auth.user_id,
        source="sync",
        message=f"Course deleted: {course_key} — {push_status}",
        device_id=auth.device_id,
        detail={
            "course_key": course_key,
            "course_no": deleted_row.course_no,
            "semester": deleted_row.semester,
            "push_job_id": push_row,
            "push_deduplicated": push_row is None,
        },
    )
    return {"deleted": 1}
