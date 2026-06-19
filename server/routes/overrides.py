"""Override endpoints: assignment local_status + course color/name/delete.

Clients call these when the user swipes to mark done / ignored / delete.
The change is written to the per-user override table and appended to the
changelog so other devices pick it up via delta-sync.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import structlog
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.dependencies import CurrentAuthDep
from server.auth.models import PushJob
from server.db import SessionDep
from server.sync.changelog import append_change
from server.sync.models import (
    AssignmentLocalStatus,
    ChangeEntityType,
    UserAssignment,
    UserAssignmentOverride,
    UserCourse,
    UserCourseOverride,
)

router = APIRouter(tags=["sync-overrides"])
logger = structlog.get_logger(__name__)


async def _enqueue_sync_trigger(session: AsyncSession, user_id, device_id) -> None:
    now = datetime.now(UTC)
    stmt = (
        pg_insert(PushJob)
        .values(
            user_id=user_id,
            dedupe_key=f"sync_trigger:{user_id}:{int(now.timestamp()) // 300}",
            channel="system",
            scenario="sync_trigger",
            fire_at=now,
            payload={"kind": "sync_trigger", "source_device_id": str(device_id) if device_id else None},
        )
        .on_conflict_do_nothing()
    )
    await session.execute(stmt)


class AssignmentOverrideRequest(BaseModel):
    local_status: Literal["none", "locally_completed", "ignored", "archived"]


class AssignmentOverrideResponse(BaseModel):
    id: int
    local_status: str
    updated_at: str


class CourseOverrideRequest(BaseModel):
    is_hidden: bool | None = None
    color_hex: str | None = None
    custom_name: str | None = None
    locale: str | None = None


class CourseOverrideResponse(BaseModel):
    id: int
    color_hex: str | None
    custom_names: dict[str, str]
    updated_at: str


class DeletedResponse(BaseModel):
    deleted: bool = True


@router.patch(
    "/assignments/{moodle_assignment_id}/override",
)
async def patch_assignment_override(
    moodle_assignment_id: int,
    payload: AssignmentOverrideRequest,
    auth: CurrentAuthDep,
    session: SessionDep,
):
    assignment = await _get_assignment_by_moodle_id(
        session, auth.user_id, moodle_assignment_id
    )
    now = datetime.now(UTC)

    if payload.local_status == "none":
        # Hard-delete the override row instead of keeping status="none".
        existing = (
            await session.execute(
                select(UserAssignmentOverride).where(
                    UserAssignmentOverride.user_id == auth.user_id,
                    UserAssignmentOverride.user_assignment_id == assignment.id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            await session.delete(existing)

        await append_change(
            session,
            user_id=auth.user_id,
            entity_type=ChangeEntityType.assignment_override.value,
            entity_id=str(assignment.id),
            operation="delete",
            payload={"local_status": "none"},
            device_id=auth.device_id,
        )

        from server.syncjobs.log_entries import log_sync

        await log_sync(session, user_id=auth.user_id, source="override",
                       message=f"Assignment override deleted: moodle_id={moodle_assignment_id}",
                       device_id=auth.device_id,
                       detail={"moodle_assignment_id": moodle_assignment_id,
                               "local_status": "none"})

        await _enqueue_sync_trigger(session, auth.user_id, auth.device_id)
        return DeletedResponse()

    stmt = (
        pg_insert(UserAssignmentOverride)
        .values(
            user_id=auth.user_id,
            user_assignment_id=assignment.id,
            local_status=payload.local_status,
            local_status_updated_at=now,
            local_status_device_id=auth.device_id,
        )
        .on_conflict_do_update(
            index_elements=["user_id", "user_assignment_id"],
            set_={
                "local_status": payload.local_status,
                "local_status_updated_at": now,
                "local_status_device_id": auth.device_id,
                "updated_at": now,
            },
        )
        .returning(UserAssignmentOverride.id)
    )
    result = await session.execute(stmt)
    override_id = result.scalar_one()

    await append_change(
        session,
        user_id=auth.user_id,
        entity_type=ChangeEntityType.assignment_override.value,
        entity_id=str(assignment.id),
        operation="upsert",
        payload={"local_status": payload.local_status},
        device_id=auth.device_id,
    )

    from server.syncjobs.log_entries import log_sync

    await log_sync(session, user_id=auth.user_id, source="override",
                   message=f"Assignment override: moodle_id={moodle_assignment_id} → {payload.local_status}",
                   device_id=auth.device_id,
                   detail={"moodle_assignment_id": moodle_assignment_id,
                           "local_status": payload.local_status})

    await _enqueue_sync_trigger(session, auth.user_id, auth.device_id)

    return AssignmentOverrideResponse(
        id=override_id,
        local_status=payload.local_status,
        updated_at=now.isoformat(),
    )


@router.patch(
    "/courses/{moodle_course_id}/override",
)
async def patch_course_override(
    moodle_course_id: str,
    payload: CourseOverrideRequest,
    auth: CurrentAuthDep,
    session: SessionDep,
):
    now = datetime.now(UTC)

    if payload.is_hidden is True:
        # Hard-delete the UserCourse row (cascades to override + skipped dates).
        course = await _get_course_by_moodle_id(
            session, auth.user_id, moodle_course_id
        )
        await session.delete(course)

        await append_change(
            session,
            user_id=auth.user_id,
            entity_type=ChangeEntityType.course.value,
            entity_id=str(course.id),
            operation="delete",
            payload=None,
            device_id=auth.device_id,
        )

        from server.syncjobs.log_entries import log_sync

        await log_sync(
            session,
            user_id=auth.user_id,
            source="override",
            message=f"Course deleted: moodle_id={moodle_course_id}",
            device_id=auth.device_id,
            detail={"moodle_course_id": moodle_course_id},
        )

        await _enqueue_sync_trigger(session, auth.user_id, auth.device_id)
        return DeletedResponse()

    if payload.is_hidden is False:
        # No-op: the course should already exist via upload.
        course = (
            await session.execute(
                select(UserCourse).where(
                    UserCourse.moodle_id == moodle_course_id,
                    UserCourse.user_id == auth.user_id,
                )
            )
        ).scalar_one_or_none()
        if course is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        existing = (
            await session.execute(
                select(UserCourseOverride).where(
                    UserCourseOverride.user_id == auth.user_id,
                    UserCourseOverride.user_course_id == course.id,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            return CourseOverrideResponse(
                id=course.id,
                color_hex=None,
                custom_names={},
                updated_at=course.updated_at.isoformat(),
            )
        return CourseOverrideResponse(
            id=existing.id,
            color_hex=existing.color_hex,
            custom_names=existing.custom_names or {},
            updated_at=existing.updated_at.isoformat(),
        )

    # Normal update path: color_hex / custom_name only.
    course = await _get_course_by_moodle_id(
        session, auth.user_id, moodle_course_id
    )

    existing = (
        await session.execute(
            select(UserCourseOverride).where(
                UserCourseOverride.user_id == auth.user_id,
                UserCourseOverride.user_course_id == course.id,
            )
        )
    ).scalar_one_or_none()

    if existing is None:
        existing = UserCourseOverride(
            user_id=auth.user_id, user_course_id=course.id
        )
        session.add(existing)

    changed_fields: list[str] = []

    if payload.color_hex is not None:
        existing.color_hex = payload.color_hex
        existing.color_hex_updated_at = now
        existing.color_hex_device_id = auth.device_id
        changed_fields.append("color_hex")

    if payload.locale and payload.custom_name is not None:
        names = dict(existing.custom_names or {})
        if payload.custom_name == "":
            names.pop(payload.locale, None)
        else:
            names[payload.locale] = payload.custom_name
        existing.custom_names = names
        existing.custom_name_updated_at = now
        existing.custom_name_device_id = auth.device_id
        changed_fields.append("custom_name")

    existing.updated_at = now
    await session.flush()

    await append_change(
        session,
        user_id=auth.user_id,
        entity_type=ChangeEntityType.course_override.value,
        entity_id=str(course.id),
        operation="upsert",
        payload={"fields": changed_fields},
        device_id=auth.device_id,
    )

    from server.syncjobs.log_entries import log_sync

    parts = []
    if payload.color_hex is not None:
        parts.append(f"color={payload.color_hex}")
    if payload.custom_name is not None:
        parts.append(f"name={payload.custom_name!r}")
    await log_sync(
        session,
        user_id=auth.user_id,
        source="override",
        message=f"Course override: moodle_id={moodle_course_id} → {', '.join(parts)}",
        device_id=auth.device_id,
        detail={"moodle_course_id": moodle_course_id, "fields": changed_fields},
    )

    await _enqueue_sync_trigger(session, auth.user_id, auth.device_id)

    return CourseOverrideResponse(
        id=existing.id,
        color_hex=existing.color_hex,
        custom_names=existing.custom_names or {},
        updated_at=now.isoformat(),
    )


async def _get_assignment_by_moodle_id(
    session: AsyncSession, user_id, moodle_assignment_id: int
) -> UserAssignment:
    row = (
        await session.execute(
            select(UserAssignment).where(
                UserAssignment.moodle_assignment_id == moodle_assignment_id,
                UserAssignment.user_id == user_id,
                UserAssignment.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return row


async def _get_course_by_moodle_id(
    session: AsyncSession, user_id, moodle_id: str
) -> UserCourse:
    row = (
        await session.execute(
            select(UserCourse).where(
                UserCourse.moodle_id == moodle_id,
                UserCourse.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return row
