"""/v3/sync — incremental change feed and full snapshot."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select

from server.auth.dependencies import CurrentAuthDep
from server.db import SessionDep
from server.sync import serializers
from server.sync.changelog import RevisionExpired, read_changes
from server.sync.models import (
    UserAssignment,
    UserAssignmentOverride,
    UserBulletinState,
    UserBulletinSubscription,
    UserCourse,
    UserCourseOverride,
    UserCourseSkippedDate,
    UserSettingsDocument,
    UserSyncState,
)

router = APIRouter(prefix="/sync", tags=["sync"])
logger = structlog.get_logger(__name__)

MAX_SYNC_LIMIT = 500


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
async def full_sync(auth: CurrentAuthDep, session: SessionDep):
    """Authoritative snapshot of all user-scoped data + current_revision.

    Runs under REPEATABLE READ so every section and the revision watermark
    come from one consistent snapshot — a write landing mid-request can't
    produce a snapshot that disagrees with its revision.
    """
    # The auth dependency already ran a query on this session, which began
    # a READ COMMITTED transaction — isolation can only be set on a fresh
    # one. Close it (it held reads only) and start the snapshot transaction.
    await session.commit()
    await session.connection(
        execution_options={"isolation_level": "REPEATABLE READ"}
    )

    async def rows(stmt):
        return (await session.execute(stmt)).scalars().all()

    user_id = auth.user_id
    state = await session.get(UserSyncState, user_id)
    courses = await rows(select(UserCourse).where(UserCourse.user_id == user_id))
    course_overrides = await rows(
        select(UserCourseOverride).where(UserCourseOverride.user_id == user_id)
    )
    skipped = await rows(
        select(UserCourseSkippedDate).where(
            UserCourseSkippedDate.user_id == user_id
        )
    )
    assignments = await rows(
        select(UserAssignment).where(UserAssignment.user_id == user_id)
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

    return {
        "current_revision": state.current_revision if state else 0,
        "courses": [serializers.course_to_dict(c) for c in courses],
        "course_overrides": [
            serializers.course_override_to_dict(o) for o in course_overrides
        ],
        "course_skipped_dates": [
            serializers.skipped_date_to_dict(s) for s in skipped
        ],
        "assignments": [serializers.assignment_to_dict(a) for a in assignments],
        "assignment_overrides": [
            serializers.assignment_override_to_dict(o)
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
