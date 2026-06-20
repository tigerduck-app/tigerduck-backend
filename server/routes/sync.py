"""/v3/sync — incremental change feed and full snapshot."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from server.auth.dependencies import CurrentAuthDep
from server.db import SessionDep
from server.sync import serializers
from server.auth.models import PushJob
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
    UserSettingsDocument,
    UserSyncState,
)
from server.syncjobs.models import SyncJob, SyncJobStatus

router = APIRouter(prefix="/sync", tags=["sync"])
logger = structlog.get_logger(__name__)

MAX_SYNC_LIMIT = 500
_CLIENT_SYNC_PUSHBACK_SECONDS = 7200  # 2 hours


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
    from server.syncjobs.log_entries import log_sync

    await log_sync(
        session,
        user_id=auth.user_id,
        source="sync",
        message="Full sync fetched",
        device_id=auth.device_id,
    )
    await _push_back_sync_jobs(session, auth.user_id)
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


class CourseUploadRequest(BaseModel):
    courses: list[CourseUploadItem]


@router.post("/courses/upload")
async def upload_courses(
    payload: CourseUploadRequest,
    auth: CurrentAuthDep,
    session: SessionDep,
):
    now = datetime.now(UTC)

    existing_keys = set(
        (await session.execute(
            select(UserCourse.course_key).where(UserCourse.user_id == auth.user_id)
        )).scalars().all()
    )

    upserted = 0
    uploaded_keys: set[str] = set()
    for c in payload.courses:
        course_key = f"client:{c.semester}:{c.course_no}"
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
        await session.execute(
            pg_insert(PushJob)
            .values(
                user_id=auth.user_id,
                dedupe_key=f"sync_trigger:{auth.user_id}:{int(now.timestamp()) // 300}",
                channel="system",
                scenario="sync_trigger",
                fire_at=now,
                payload={
                    "kind": "sync_trigger",
                    "source_device_id": str(auth.device_id) if auth.device_id else None,
                },
            )
            .on_conflict_do_nothing()
        )

    await _push_back_sync_jobs(session, auth.user_id)
    return {"upserted": upserted}


@router.delete("/courses/{course_key:path}")
async def delete_course(
    course_key: str,
    auth: CurrentAuthDep,
    session: SessionDep,
):
    del_stmt = (
        delete(UserCourse)
        .where(
            UserCourse.user_id == auth.user_id,
            UserCourse.course_key == course_key,
        )
        .returning(UserCourse.id)
    )
    result = await session.execute(del_stmt)
    deleted_row = result.scalar_one_or_none()
    if deleted_row is None:
        return {"deleted": 0}

    state = await lock_sync_state(session, auth.user_id)
    await append_change(
        session,
        user_id=auth.user_id,
        entity_type=ChangeEntityType.course.value,
        entity_id=str(deleted_row),
        operation="delete",
        payload={"course_key": course_key},
        device_id=auth.device_id,
        locked_state=state,
    )
    now = datetime.now(UTC)
    await session.execute(
        pg_insert(PushJob)
        .values(
            user_id=auth.user_id,
            dedupe_key=f"sync_trigger:{auth.user_id}:{int(now.timestamp()) // 300}",
            channel="system",
            scenario="sync_trigger",
            fire_at=now,
            payload={
                "kind": "sync_trigger",
                "source_device_id": str(auth.device_id) if auth.device_id else None,
            },
        )
        .on_conflict_do_nothing()
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
    await _push_back_sync_jobs(session, auth.user_id)
    return {"upserted": upserted}


async def _read_full_snapshot(session, user_id):
    async def rows(stmt):
        return (await session.execute(stmt)).scalars().all()

    state = await session.get(UserSyncState, user_id)
    courses = await rows(
        select(UserCourse).where(UserCourse.user_id == user_id)
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

    logger.info(
        "sync.full_snapshot",
        user_id=str(user_id),
        course_count=len(courses),
        course_nos=sorted(c.course_no or "" for c in courses),
        course_semesters=sorted(set(c.semester for c in courses)),
    )

    return {
        "current_revision": state.current_revision if state else 0,
        "courses": [serializers.course_to_dict(c) for c in courses],
        "course_overrides": [
            serializers.course_override_to_dict(
                o, moodle_id=_course_pk_to_moodle.get(o.user_course_id)
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
