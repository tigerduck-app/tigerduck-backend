"""Course uploads: the first-run bulk upload and the per-course upsert
that follows it."""

from __future__ import annotations
from datetime import UTC, datetime, timedelta
from fastapi import APIRouter, BackgroundTasks, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from server.auth.dependencies import CurrentAuthDep
from server.db import SessionDep
from server.auth.models import PushDelivery, PushDeliveryStatus, PushJob, PushJobStatus, User, UserDevice
from server.sync.changelog import RevisionExpired, append_change, lock_sync_state, read_changes
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
from ._shared import _push_back_sync_jobs, _trigger_push_tick, logger

router = APIRouter(prefix="/sync", tags=["sync"])


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

    # A reset tombstone does not bind the device that wrote it. That reset
    # cleared the term precisely so this upload could replace it, and the
    # roster the portal returned seconds later is the replacement -- so
    # retire the rows it covers and let the upsert through. Anything the
    # reset dropped that this upload does not mention keeps its tombstone
    # and goes on binding every device, this one included.
    payload_keys = {f"client:{c.semester}:{c.course_no}" for c in payload.courses}
    if auth.device_id and payload_keys:
        await session.execute(
            delete(UserCourseTombstone).where(
                UserCourseTombstone.user_id == auth.user_id,
                UserCourseTombstone.deleted_by_reset.is_(True),
                UserCourseTombstone.deleted_by_device_id == auth.device_id,
                UserCourseTombstone.course_key.in_(payload_keys),
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
    override_items = [i for i in payload.course_overrides if i.color_hex]
    courses_by_key: dict[str, UserCourse] = {}
    overrides_by_course_id: dict[int, UserCourseOverride] = {}
    if override_items:
        courses_by_key = {
            c.course_key: c
            for c in (
                await session.execute(
                    select(UserCourse).where(
                        UserCourse.user_id == auth.user_id,
                        UserCourse.course_key.in_(
                            {i.course_key for i in override_items}
                        ),
                    )
                )
            ).scalars()
        }
        overrides_by_course_id = {
            o.user_course_id: o
            for o in (
                await session.execute(
                    select(UserCourseOverride).where(
                        UserCourseOverride.user_id == auth.user_id,
                        UserCourseOverride.user_course_id.in_(
                            [c.id for c in courses_by_key.values()]
                        ),
                    )
                )
            ).scalars()
        }
    for item in override_items:
        course = courses_by_key.get(item.course_key)
        if course is None:
            continue
        override = overrides_by_course_id.get(course.id)
        if override is None:
            override = UserCourseOverride(
                user_id=auth.user_id, user_course_id=course.id
            )
            session.add(override)
            await session.flush()
            overrides_by_course_id[course.id] = override
        if override.color_hex is None:
            override.color_hex = item.color_hex
            override.color_hex_updated_at = now
            override.color_hex_device_id = auth.device_id
            overrides_applied += 1
            logger.debug(
                "sync.course_color_set",
                course_key=item.course_key,
                color_hex=item.color_hex,
                device_id=str(auth.device_id) if auth.device_id else None,
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
