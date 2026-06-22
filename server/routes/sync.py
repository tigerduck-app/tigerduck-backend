"""/v3/sync — incremental change feed and full snapshot."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import asyncio

import structlog
from fastapi import APIRouter, BackgroundTasks, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from server.auth.dependencies import CurrentAuthDep
from server.db import SessionDep
from server.sync import serializers
from server.auth.models import PushDelivery, PushDeliveryStatus, PushJob, PushJobStatus, User, UserDevice
from server.sync.changelog import RevisionExpired, append_change, lock_sync_state, read_changes
from server.sync.poll_tracker import record_poll
from server.sync.upload import InitialUploadRequest, process_initial_upload
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
from server.syncjobs.models import SyncJob, SyncJobStatus

router = APIRouter(prefix="/sync", tags=["sync"])
logger = structlog.get_logger(__name__)

MAX_SYNC_LIMIT = 500
_CLIENT_SYNC_PUSHBACK_SECONDS = 7200  # 2 hours

_push_tick_lock = asyncio.Lock()


async def _trigger_push_tick(request: Request) -> None:
    """Run the push pipeline tick immediately so sync_trigger pushes
    are delivered within seconds instead of waiting up to 30s."""
    worker = getattr(request.app.state, "push_worker", None)
    if worker is None:
        return
    if _push_tick_lock.locked():
        return
    async with _push_tick_lock:
        from server.push.pipeline import run_push_tick
        await run_push_tick(worker)


async def _cancel_pending_deliveries_for_device(session, user_id, device_id) -> None:
    """Cancel ALL pending pushes for *device_id*.

    When the device just did a full sync it already has fresh data — no
    need for sync_trigger, schedule, or reminder pushes.

    Two passes:
    1. Device-targeted jobs (schedule pushes with job.device_id set):
       cancel the entire job since it only targets this device.
    2. User-scoped jobs (sync_trigger etc. with device_id NULL):
       skip only this device's materialized deliveries so other devices
       still receive the push.
    """
    now = datetime.now(UTC)
    device_row = (await session.execute(
        select(UserDevice.id).where(
            UserDevice.id == device_id,
            UserDevice.user_id == user_id,
            UserDevice.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    if device_row is None:
        return

    cancelled_jobs = await session.execute(
        update(PushJob)
        .where(
            PushJob.user_id == user_id,
            PushJob.device_id == device_row,
            PushJob.status == PushJobStatus.pending.value,
        )
        .values(status=PushJobStatus.cancelled.value, cancelled_at=now)
    )
    job_count = cancelled_jobs.rowcount or 0

    pending_job_ids = (await session.execute(
        select(PushJob.id).where(
            PushJob.user_id == user_id,
            PushJob.device_id.is_(None),
            PushJob.status.in_([PushJobStatus.pending.value, PushJobStatus.processing.value]),
        )
    )).scalars().all()
    delivery_count = 0
    if pending_job_ids:
        result = await session.execute(
            update(PushDelivery)
            .where(
                PushDelivery.push_job_id.in_(pending_job_ids),
                PushDelivery.device_id == device_row,
                PushDelivery.status == PushDeliveryStatus.pending.value,
            )
            .values(
                status=PushDeliveryStatus.skipped.value,
                failure_code="device_already_synced",
            )
        )
        delivery_count = result.rowcount or 0

    if job_count or delivery_count:
        logger.info(
            "sync.cancelled_pushes_for_device",
            user_id=str(user_id),
            device_id=str(device_id),
            jobs_cancelled=job_count,
            deliveries_skipped=delivery_count,
        )


async def _push_back_sync_jobs(session, user_id, *, seconds: int = _CLIENT_SYNC_PUSHBACK_SECONDS) -> None:
    """Push all pending sync jobs for *user_id* to ``now + seconds``.

    Called after a client sync or upload so the server's own sync cycle
    defers — the client just delivered fresh data, no need for the server
    to re-fetch from Moodle immediately.
    """
    run_after = datetime.now(UTC) + timedelta(seconds=seconds)
    await session.execute(
        update(SyncJob)
        .where(
            SyncJob.user_id == user_id,
            SyncJob.status == SyncJobStatus.pending.value,
        )
        .values(run_after=run_after, attempts=0)
    )


@router.get("/revision")
async def poll_revision(auth: CurrentAuthDep, session: SessionDep):
    """Lightweight poll endpoint — 10s interval while foregrounded.

    Returns just the current revision number so the client can decide
    whether a full sync is needed.  Records the poll so the push
    pipeline skips this device (it will pick up changes via polling).
    """
    if auth.device_id:
        device = await session.get(UserDevice, auth.device_id)
        if device:
            record_poll(str(auth.user_id), device.client_device_id)
            device.last_seen_at = datetime.now(UTC)
            await session.commit()
    state = await session.get(UserSyncState, auth.user_id)
    return {"revision": state.current_revision if state else 0}


@router.get("")
async def incremental_sync(
    auth: CurrentAuthDep,
    session: SessionDep,
    since_revision: int = Query(ge=0),
    limit: int = Query(default=MAX_SYNC_LIMIT, ge=1, le=MAX_SYNC_LIMIT),
):
    try:
        page = await read_changes(
            session,
            user_id=auth.user_id,
            since_revision=since_revision,
            limit=limit,
        )
    except RevisionExpired as expired:
        return JSONResponse(
            status_code=410,
            content={
                "error": "sync_revision_expired",
                "min_available_revision": expired.min_available_revision,
                "current_revision": expired.current_revision,
                "full_sync_required": True,
            },
        )
    return {
        "current_revision": page.current_revision,
        "returned_until_revision": page.returned_until_revision,
        "has_more": page.has_more,
        "changes": [
            {
                "revision": c.revision,
                "entity_type": c.entity_type,
                "entity_id": c.entity_id,
                "operation": c.operation,
                "payload": c.payload,
            }
            for c in page.changes
        ],
    }


@router.post("/initial-upload")
async def initial_upload(
    payload: InitialUploadRequest, auth: CurrentAuthDep, session: SessionDep
):
    """First-login import of the device's local data. Idempotent — safe to
    re-send the whole body after a crash."""
    result = await process_initial_upload(
        session,
        user_id=auth.user_id,
        device_id=auth.device_id,
        payload=payload,
    )
    await log_sync(
        session,
        user_id=auth.user_id,
        source="sync",
        message=f"Initial upload: {result.counts}",
        device_id=auth.device_id,
        detail=result.counts,
    )
    return {
        "counts": result.counts,
        "current_revision": result.current_revision,
    }


@router.get("/full")
async def full_sync(auth: CurrentAuthDep, session: SessionDep, request: Request):
    """Authoritative snapshot of all user-scoped data + current_revision.

    Runs under REPEATABLE READ so every section and the revision watermark
    come from one consistent snapshot — a write landing mid-request can't
    produce a snapshot that disagrees with its revision.

    Uses a dedicated session rather than the request-scoped one: mutating
    the shared session's isolation level (and committing it mid-request)
    would leak surprising state into the dependency's commit/rollback
    handling and the pooled connection.
    """
    await log_sync(
        session,
        user_id=auth.user_id,
        source="sync",
        message="Full sync fetched",
        device_id=auth.device_id,
    )
    await _push_back_sync_jobs(session, auth.user_id)

    if auth.device_id:
        await _cancel_pending_deliveries_for_device(session, auth.user_id, auth.device_id)
        device = await session.get(UserDevice, auth.device_id)
        if device:
            device.last_seen_at = datetime.now(UTC)

    await session.commit()

    factory = request.app.state.session_factory
    async with factory() as snap_session:
        await snap_session.connection(
            execution_options={"isolation_level": "REPEATABLE READ"}
        )
        return await _read_full_snapshot(snap_session, auth.user_id)


class CourseUploadItem(BaseModel):
    semester: str
    course_no: str
    course_name: str
    course_name_en: str | None = None
    moodle_id: str | None = None
    credits: float | None = None
    classroom: str | None = None
    instructors: list[str] = []
    schedule_json: dict | list = {}
    classroom_map: dict = {}


class CourseOverrideUploadItem(BaseModel):
    course_key: str
    color_hex: str | None = Field(default=None, max_length=16)


class CourseUploadRequest(BaseModel):
    courses: list[CourseUploadItem]
    course_overrides: list[CourseOverrideUploadItem] = Field(
        default_factory=list, max_length=500
    )
    force_keys: list[str] = Field(
        default_factory=list, max_length=50,
        description="Course keys whose tombstones should be cleared (explicit user re-add).",
    )


@router.post("/courses/upload")
async def upload_courses(
    payload: CourseUploadRequest,
    auth: CurrentAuthDep,
    session: SessionDep,
    request: Request,
    background_tasks: BackgroundTasks,
):
    now = datetime.now(UTC)

    existing_keys = set(
        (await session.execute(
            select(UserCourse.course_key).where(UserCourse.user_id == auth.user_id)
        )).scalars().all()
    )

    # Clear tombstones for explicitly re-added courses (user action).
    if payload.force_keys:
        await session.execute(
            delete(UserCourseTombstone).where(
                UserCourseTombstone.user_id == auth.user_id,
                UserCourseTombstone.course_key.in_(payload.force_keys),
            )
        )

    # Tombstoned courses must not be resurrected by a bulk portal upload.
    tombstoned_keys = set(
        (await session.execute(
            select(UserCourseTombstone.course_key).where(
                UserCourseTombstone.user_id == auth.user_id,
            )
        )).scalars().all()
    )

    upserted = 0
    skipped = 0
    uploaded_keys: set[str] = set()
    for c in payload.courses:
        course_key = f"client:{c.semester}:{c.course_no}"
        if course_key in tombstoned_keys:
            skipped += 1
            continue
        uploaded_keys.add(course_key)
        moodle_id = c.moodle_id or f"{c.semester}{c.course_no}"
        stmt = (
            pg_insert(UserCourse)
            .values(
                user_id=auth.user_id,
                semester=c.semester,
                course_key=course_key,
                source="ntust_portal",
                course_no=c.course_no,
                course_name=c.course_name,
                course_name_en=c.course_name_en,
                moodle_id=moodle_id,
                credits=c.credits,
                classroom=c.classroom,
                instructors=c.instructors,
                schedule_json=c.schedule_json,
                classroom_map=c.classroom_map,
                fetched_at=now,
                last_seen_at=now,
                updated_by_device_id=auth.device_id,
            )
            .on_conflict_do_update(
                index_elements=["user_id", "semester", "course_key"],
                set_={
                    "course_name": c.course_name,
                    "course_name_en": c.course_name_en,
                    "moodle_id": moodle_id,
                    "credits": c.credits,
                    "classroom": c.classroom,
                    "instructors": c.instructors,
                    "schedule_json": c.schedule_json,
                    "classroom_map": c.classroom_map,
                    "last_seen_at": now,
                    "updated_at": now,
                    "version": UserCourse.__table__.c.version + 1,
                    "updated_by_device_id": auth.device_id,
                },
            )
        )
        await session.execute(stmt)
        upserted += 1

    new_keys = uploaded_keys - existing_keys
    logger.info(
        "sync.upload_courses",
        user_id=str(auth.user_id),
        device_id=str(auth.device_id) if auth.device_id else None,
        upserted=upserted,
        new_keys=sorted(new_keys) if new_keys else [],
        uploaded_keys=sorted(uploaded_keys),
    )
    if new_keys:
        state = await lock_sync_state(session, auth.user_id)
        new_rows = (await session.execute(
            select(UserCourse.id, UserCourse.course_key)
            .where(
                UserCourse.user_id == auth.user_id,
                UserCourse.course_key.in_(new_keys),
            )
        )).all()
        for row in new_rows:
            await append_change(
                session,
                user_id=auth.user_id,
                entity_type=ChangeEntityType.course.value,
                entity_id=str(row.id),
                operation="upsert",
                payload={"course_key": row.course_key},
                device_id=auth.device_id,
                locked_state=state,
            )
        now = datetime.now(UTC)
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
            await log_sync(
                session, user_id=auth.user_id, source="push",
                message=f"sync_trigger push job #{push_row} queued ({len(new_keys)} new courses)",
                device_id=auth.device_id,
                detail={"push_job_id": push_row, "new_keys": sorted(new_keys)},
            )
            background_tasks.add_task(_trigger_push_tick, request)

    overrides_applied = 0
    for item in payload.course_overrides:
        if not item.color_hex:
            continue
        course = (
            await session.execute(
                select(UserCourse).where(
                    UserCourse.user_id == auth.user_id,
                    UserCourse.course_key == item.course_key,
                )
            )
        ).scalar_one_or_none()
        if course is None:
            continue
        override = (
            await session.execute(
                select(UserCourseOverride).where(
                    UserCourseOverride.user_id == auth.user_id,
                    UserCourseOverride.user_course_id == course.id,
                )
            )
        ).scalar_one_or_none()
        if override is None:
            override = UserCourseOverride(
                user_id=auth.user_id, user_course_id=course.id
            )
            session.add(override)
            await session.flush()
        if override.color_hex is None:
            override.color_hex = item.color_hex
            override.color_hex_updated_at = now
            override.color_hex_device_id = auth.device_id
            overrides_applied += 1
            logger.debug(
                "sync.course_color_set",
                course_key=item.course_key,
                color_hex=item.color_hex,
                device_id=str(auth.device_id),
            )
        elif override.color_hex != item.color_hex:
            logger.debug(
                "sync.course_color_skipped",
                course_key=item.course_key,
                server_hex=override.color_hex,
                client_hex=item.color_hex,
            )

    course_nos = [c.course_no for c in payload.courses]
    await log_sync(
        session,
        user_id=auth.user_id,
        source="sync",
        message=f"Courses uploaded: {upserted} upserted, {len(new_keys)} new, {skipped} tombstoned, {overrides_applied} colors set",
        device_id=auth.device_id,
        detail={
            "upserted": upserted,
            "skipped_tombstoned": skipped,
            "new_keys": sorted(new_keys) if new_keys else [],
            "overrides_applied": overrides_applied,
            "course_nos": course_nos,
        },
    )
    await _push_back_sync_jobs(session, auth.user_id)
    if auth.device_id:
        device = await session.get(UserDevice, auth.device_id)
        if device:
            device.last_seen_at = datetime.now(UTC)
    await session.commit()
    return {"upserted": upserted, "skipped_tombstoned": skipped, "overrides_applied": overrides_applied}


@router.delete("/courses")
async def delete_all_courses(
    auth: CurrentAuthDep,
    session: SessionDep,
    request: Request,
    background_tasks: BackgroundTasks,
):
    """Wipe all courses for the user. Used by 'reset course timetable'."""
    now = datetime.now(UTC)

    rows = (await session.execute(
        delete(UserCourse)
        .where(UserCourse.user_id == auth.user_id)
        .returning(UserCourse.id, UserCourse.course_key, UserCourse.semester, UserCourse.course_no)
    )).all()
    if not rows:
        return {"deleted": 0}

    # Full reset: clear ALL tombstones so the immediate re-upload isn't
    # blocked. courses_reset_at signals other devices that a reset happened.
    await session.execute(
        delete(UserCourseTombstone).where(
            UserCourseTombstone.user_id == auth.user_id,
        )
    )

    await session.execute(
        update(User).where(User.id == auth.user_id).values(courses_reset_at=now)
    )

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
        message=f"All courses deleted: {len(rows)} removed — {push_status}",
        device_id=auth.device_id,
        detail={"deleted": len(rows), "course_keys": deleted_keys},
    )
    logger.info(
        "sync.delete_all_courses",
        user_id=str(auth.user_id),
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


async def _read_full_snapshot(session, user_id):
    async def rows(stmt):
        return (await session.execute(stmt)).scalars().all()

    state = await session.get(UserSyncState, user_id)
    user = await session.get(User, user_id)
    courses = await rows(
        select(UserCourse).where(UserCourse.user_id == user_id)
    )
    tombstones = await rows(
        select(UserCourseTombstone).where(
            UserCourseTombstone.user_id == user_id,
            UserCourseTombstone.deleted_at > func.now() - text("interval '30 days'"),
        )
    )
    course_overrides = await rows(
        select(UserCourseOverride).where(UserCourseOverride.user_id == user_id)
    )
    skipped = await rows(
        select(UserCourseSkippedDate).where(
            UserCourseSkippedDate.user_id == user_id,
            UserCourseSkippedDate.deleted_at.is_(None),
        )
    )
    assignments = await rows(
        select(UserAssignment).where(
            UserAssignment.user_id == user_id,
            UserAssignment.deleted_at.is_(None),
        )
    )
    assignment_overrides = await rows(
        select(UserAssignmentOverride).where(
            UserAssignmentOverride.user_id == user_id
        )
    )
    settings_docs = await rows(
        select(UserSettingsDocument).where(
            UserSettingsDocument.user_id == user_id,
            UserSettingsDocument.deleted_at.is_(None),
        )
    )
    subscriptions = await rows(
        select(UserBulletinSubscription).where(
            UserBulletinSubscription.user_id == user_id,
            UserBulletinSubscription.deleted_at.is_(None),
        )
    )
    bulletin_states = await rows(
        select(UserBulletinState).where(UserBulletinState.user_id == user_id)
    )

    _pk_to_moodle = {a.id: a.moodle_assignment_id for a in assignments}
    _course_pk_to_moodle = {c.id: c.moodle_id for c in courses}
    _course_pk_to_no = {c.id: c.course_no for c in courses}

    logger.info(
        "sync.full_snapshot",
        user_id=str(user_id),
        course_count=len(courses),
        course_nos=sorted(c.course_no or "" for c in courses),
        course_semesters=sorted(set(c.semester for c in courses)),
    )

    _iso = serializers._iso
    return {
        "current_revision": state.current_revision if state else 0,
        "courses_reset_at": _iso(user.courses_reset_at) if user else None,
        "course_tombstones": [
            {
                "course_key": t.course_key,
                "course_no": t.course_no,
                "semester": t.semester,
                "deleted_at": _iso(t.deleted_at),
            }
            for t in tombstones
        ],
        "courses": [serializers.course_to_dict(c) for c in courses],
        "course_overrides": [
            serializers.course_override_to_dict(
                o,
                moodle_id=_course_pk_to_moodle.get(o.user_course_id),
                course_no=_course_pk_to_no.get(o.user_course_id),
            )
            for o in course_overrides
        ],
        "course_skipped_dates": [
            serializers.skipped_date_to_dict(s) for s in skipped
        ],
        "assignments": [serializers.assignment_to_dict(a) for a in assignments],
        "assignment_overrides": [
            serializers.assignment_override_to_dict(
                o,
                moodle_assignment_id=_pk_to_moodle.get(o.user_assignment_id),
            )
            for o in assignment_overrides
        ],
        "settings_documents": [
            serializers.settings_document_to_dict(d) for d in settings_docs
        ],
        "bulletin_subscriptions": [
            serializers.subscription_to_dict(s) for s in subscriptions
        ],
        "bulletin_states": [
            serializers.bulletin_state_to_dict(s) for s in bulletin_states
        ],
    }
