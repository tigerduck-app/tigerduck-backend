"""Reading sync state: the revision watermark, the incremental change
feed, and the full snapshot a client falls back to when its revision is
too old to catch up from the change log."""

from __future__ import annotations
from datetime import UTC, datetime, timedelta
from fastapi import APIRouter, BackgroundTasks, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import delete, func, select, text, update
from server.auth.dependencies import CurrentAuthDep
from server.db import SessionDep
from server.sync import serializers
from server.auth.models import PushDelivery, PushDeliveryStatus, PushJob, PushJobStatus, User, UserDevice
from server.sync.changelog import RevisionExpired, append_change, lock_sync_state, read_changes
from server.sync.poll_tracker import record_poll
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
from ._shared import MAX_SYNC_LIMIT, _cancel_pending_deliveries_for_device, _push_back_sync_jobs, logger

router = APIRouter(prefix="/sync", tags=["sync"])


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
