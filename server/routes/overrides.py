"""Override endpoints: assignment local_status + course is_hidden.

Clients call these when the user swipes to mark done / ignored / hidden.
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


class AssignmentOverrideRequest(BaseModel):
    local_status: Literal["none", "locally_completed", "ignored", "archived"]


class AssignmentOverrideResponse(BaseModel):
    id: int
    local_status: str
    updated_at: str


class CourseOverrideRequest(BaseModel):
    is_hidden: bool


class CourseOverrideResponse(BaseModel):
    id: int
    is_hidden: bool
    updated_at: str


@router.patch(
    "/assignments/{assignment_id}/override",
    response_model=AssignmentOverrideResponse,
)
async def patch_assignment_override(
    assignment_id: int,
    payload: AssignmentOverrideRequest,
    auth: CurrentAuthDep,
    session: SessionDep,
):
    assignment = await _get_assignment(session, auth.user_id, assignment_id)
    now = datetime.now(UTC)

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

    return AssignmentOverrideResponse(
        id=override_id,
        local_status=payload.local_status,
        updated_at=now.isoformat(),
    )


@router.patch(
    "/courses/{course_id}/override",
    response_model=CourseOverrideResponse,
)
async def patch_course_override(
    course_id: int,
    payload: CourseOverrideRequest,
    auth: CurrentAuthDep,
    session: SessionDep,
):
    course = await _get_course(session, auth.user_id, course_id)
    now = datetime.now(UTC)

    stmt = (
        pg_insert(UserCourseOverride)
        .values(
            user_id=auth.user_id,
            user_course_id=course.id,
            is_hidden=payload.is_hidden,
            is_hidden_updated_at=now,
            is_hidden_device_id=auth.device_id,
        )
        .on_conflict_do_update(
            index_elements=["user_id", "user_course_id"],
            set_={
                "is_hidden": payload.is_hidden,
                "is_hidden_updated_at": now,
                "is_hidden_device_id": auth.device_id,
                "updated_at": now,
            },
        )
        .returning(UserCourseOverride.id)
    )
    result = await session.execute(stmt)
    override_id = result.scalar_one()

    await append_change(
        session,
        user_id=auth.user_id,
        entity_type=ChangeEntityType.course_override.value,
        entity_id=str(course.id),
        operation="upsert",
        payload={"is_hidden": payload.is_hidden},
        device_id=auth.device_id,
    )

    return CourseOverrideResponse(
        id=override_id,
        is_hidden=payload.is_hidden,
        updated_at=now.isoformat(),
    )


async def _get_assignment(
    session: AsyncSession, user_id, assignment_id: int
) -> UserAssignment:
    row = (
        await session.execute(
            select(UserAssignment).where(
                UserAssignment.id == assignment_id,
                UserAssignment.user_id == user_id,
                UserAssignment.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return row


async def _get_course(
    session: AsyncSession, user_id, course_id: int
) -> UserCourse:
    row = (
        await session.execute(
            select(UserCourse).where(
                UserCourse.id == course_id,
                UserCourse.user_id == user_id,
                UserCourse.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return row
