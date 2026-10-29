"""Reading sync state: the revision watermark, the incremental change
feed, and the full snapshot a client falls back to when its revision is
too old to catch up from the change log."""

from __future__ import annotations
from datetime import UTC, datetime, timedelta
from fastapi import APIRouter, BackgroundTasks, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import delete, func, select, text, update
from server.academic_calendar.models import UserHolidayOverride
from server.auth.dependencies import CurrentAuthDep
from server.db import SessionDep
from server.sync import serializers
from server.auth.models import PushDelivery, PushDeliveryStatus, PushJob, PushJobStatus, User, UserDevice
from server.sync.changelog import RevisionExpired, append_change, lock_sync_state, read_changes
from server.sync import membership
from server.sync.merge import ClockProtocolDep, to_clock
from server.sync.models import (
    ChangeEntityType,
    UserAssignment,
    UserAssignmentOverride,
    UserChangeLog,
    UserCourse,
    UserCourseOverride,
    UserCourseSkippedDate,
    UserCourseTombstone,
    UserSettingsDocument,
    UserSyncState,
)
from server.syncjobs.log_entries import log_sync
from ._shared import MAX_SYNC_LIMIT, _cancel_pending_deliveries_for_device, logger

router = APIRouter(prefix="/sync", tags=["sync"])


@router.get("/revision")
async def poll_revision(auth: CurrentAuthDep, session: SessionDep):
    """Lightweight poll endpoint, called while the app is in the
    foreground.

    Returns just the current revision number so the client can decide
    whether a full sync is needed, and marks the device seen.
    """
    if auth.device_id:
        device = await session.get(UserDevice, auth.device_id)
        if device:
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
async def full_sync(
    auth: CurrentAuthDep, session: SessionDep, request: Request, speaks_clock: ClockProtocolDep
):
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
        return await _read_full_snapshot(
            snap_session, auth.user_id, auth.device_id, speaks_clock=speaks_clock
        )
async def _read_full_snapshot(session, user_id, device_id, *, speaks_clock=False):
    async def rows(stmt):
        return (await session.execute(stmt)).scalars().all()

    # Before the first query, which fixes what the snapshot sees: a write
    # whose clock header is earlier than this is in it.
    server_hlc = to_clock(datetime.now(UTC))
    state = await session.get(UserSyncState, user_id)
    user = await session.get(User, user_id)
    courses = await rows(
        select(UserCourse).where(UserCourse.user_id == user_id)
    )
    # Protocol 2 orders every membership edit by these stamps, so it gets
    # every tombstone; released apps keep the last 30 days.
    tombstone_scope = [UserCourseTombstone.user_id == user_id]
    if not speaks_clock:
        tombstone_scope.append(
            UserCourseTombstone.deleted_at > func.now() - text("interval '30 days'")
        )
    tombstones = await rows(select(UserCourseTombstone).where(*tombstone_scope))
    resets = await membership.Resets.load(session, user_id)
    # In creation order: a course taken again has an override per term under
    # one course number, and the released apps keep the later row's name and,
    # on Android 2.3.1, the first row's colour, so a new term must not move up.
    course_overrides = await rows(
        select(UserCourseOverride)
        .where(UserCourseOverride.user_id == user_id)
        .order_by(UserCourseOverride.id)
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
    # Holidays themselves are school-wide and arrive over the public
    # calendar feed; only the user's "notify me anyway" exceptions are
    # user-scoped, so only those belong in this snapshot.
    holiday_overrides = await rows(
        select(UserHolidayOverride).where(UserHolidayOverride.user_id == user_id)
    )

    _pk_to_moodle = {a.id: a.moodle_assignment_id for a in assignments}
    _pk_to_moodle |= await _keys_for_marks_on_deleted_rows(
        session, user_id, assignment_overrides, _pk_to_moodle
    )
    _course_pk_to_moodle = {c.id: c.moodle_id for c in courses}
    _course_pk_to_no = {c.id: c.course_no for c in courses}
    _course_pk_to_semester = {c.id: c.semester for c in courses}

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
        "server_hlc": server_hlc,
        "courses_reset_at": _iso(user.courses_reset_at) if user else None,
        # For released apps: a reset tombstone does not bind the device that
        # wrote it (see `upload_courses`), and the client applies the same
        # rule: between its reset's DELETE and the re-upload that releases
        # them, a poll would otherwise read its own tombstones as "hide
        # everything".
        # Authorship is resolved here rather than by sending the device id,
        # so the client needs nothing it does not already have.
        #
        # Nor does a single delete's tombstone of a course added by hand,
        # but the apps hide every other tombstone, so its author never gets
        # it: that device took the course off itself, and hiding it there
        # would keep the roster that lists it later from going up.
        "course_tombstones": (
            [_tombstone_by_stamp(t) for t in tombstones]
            if speaks_clock
            else _released_tombstones(tombstones, device_id, {c.course_key for c in courses})
        ),
        "courses_reset_hlc": serializers.hlc(resets.full),
        "term_resets": {term: to_clock(at) for term, at in sorted(resets.terms.items())},
        "holiday_overrides": [
            {
                "holiday_id": o.holiday_id,
                "notify": o.notify,
                "hlc": serializers.hlc(o.updated_at),
            }
            for o in holiday_overrides
        ],
        "courses": [serializers.course_to_dict(c) for c in courses],
        "course_overrides": [
            serializers.course_override_to_dict(
                o,
                moodle_id=_course_pk_to_moodle.get(o.user_course_id),
                course_no=_course_pk_to_no.get(o.user_course_id),
                semester=_course_pk_to_semester.get(o.user_course_id),
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
        # No bulletin sections: subscriptions belong to one device and read
        # state is never shared, so neither is part of TigerSync.
    }


def _tombstone_by_stamp(t: UserCourseTombstone) -> dict:
    return {
        "course_key": t.course_key,
        "course_no": t.course_no,
        "semester": t.semester,
        "deleted_at": serializers._iso(t.deleted_at),
        "hlc": to_clock(t.deleted_at),
        "kind": membership.tombstone_kind(t),
    }


def _released_tombstones(tombstones, device_id, listed: set[str]) -> list[dict]:
    """Today's tombstones for the released apps, which hide every course a
    tombstone names. A removal kept beside a roster row hides nothing, so
    they do not get it."""
    return [
        {
            "course_key": t.course_key,
            "course_no": t.course_no,
            "semester": t.semester,
            "deleted_at": serializers._iso(t.deleted_at),
            "deleted_by_reset": t.deleted_by_reset,
            "deleted_by_this_device": (
                device_id is not None and t.deleted_by_device_id == device_id
            ),
        }
        for t in tombstones
        if not (
            t.user_added
            and not t.deleted_by_reset
            and (
                t.course_key in listed
                or (device_id is not None and t.deleted_by_device_id == device_id)
            )
        )
    ]


async def _keys_for_marks_on_deleted_rows(
    session, user_id, overrides, live_keys: dict[int, int]
) -> dict[int, int]:
    """Moodle assignment ids for marks whose row the server soft-deleted.

    Both apps read a mark they cannot key as cleared, yet a fetch soft-deletes
    rows the apps still show, such as a TA's course. Such a mark is keyed only
    while no live row has its assignment, and only the newest one: beside a
    live row, an older mark may be one the user has cleared there since. So a
    mark the server's row outlived, such as a placeholder's from before the
    server fetched the assignment, stays unkeyed.
    """
    orphaned = {o.user_assignment_id for o in overrides} - live_keys.keys()
    if not orphaned:
        return {}
    server_rows_gone = (
        select(
            UserAssignment.moodle_assignment_id,
            func.max(UserAssignment.deleted_at).label("deleted_at"),
        )
        .where(
            UserAssignment.user_id == user_id,
            UserAssignment.moodle_course_id != 0,
            UserAssignment.deleted_at.is_not(None),
        )
        .group_by(UserAssignment.moodle_assignment_id)
        .subquery()
    )
    deleted_rows = {
        row.id: row
        for row in await session.execute(
            select(
                UserAssignment.id,
                UserAssignment.moodle_assignment_id,
                UserAssignment.deleted_at,
                server_rows_gone.c.deleted_at.label("server_row_deleted_at"),
            )
            .outerjoin(
                server_rows_gone,
                server_rows_gone.c.moodle_assignment_id
                == UserAssignment.moodle_assignment_id,
            )
            .where(
                UserAssignment.user_id == user_id,
                UserAssignment.id.in_(orphaned),
            )
        )
    }
    live_ids = set(live_keys.values())
    newest: dict[int, UserAssignmentOverride] = {}
    for override in overrides:
        row = deleted_rows.get(override.user_assignment_id)
        if row is None or row.moodle_assignment_id in live_ids:
            continue
        if _hidden_since_received(override, row):
            continue
        kept = newest.get(row.moodle_assignment_id)
        if kept is None or _marked_at(override) > _marked_at(kept):
            newest[row.moodle_assignment_id] = override
    return {o.user_assignment_id: moodle_id for moodle_id, o in newest.items()}


def _hidden_since_received(override: UserAssignmentOverride, row) -> bool:
    # A server row was live after this row went and after the mark reached the
    # server, so the mark went out unkeyed and both apps dropped it; a clear
    # made on that row may also have left it in place. A note edit since does
    # not count as the mark arriving.
    received = override.local_status_received_at or override.updated_at
    return (
        row.server_row_deleted_at is not None
        and row.server_row_deleted_at > max(row.deleted_at, received)
    )


_NEVER = datetime.min.replace(tzinfo=UTC)


def _marked_at(override: UserAssignmentOverride) -> tuple[datetime, int]:
    # Without a status time the row carries no mark, so any mark outranks it.
    return (override.local_status_updated_at or _NEVER, override.id)
