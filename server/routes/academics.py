"""/v3/courses and /v3/assignments — user-scoped academic data.

Reads serve the backend copy (client-uploaded snapshot in Phase 2; kept
fresh by server-side sync from Phase 3 on). Override writes use per-field
merge: presence of `<field>_updated_at` marks an edit (so explicit nulls
can clear nullable fields); timestamps are clamped against clock skew.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import structlog
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from server.auth.dependencies import CurrentAuthDep
from server.db import SessionDep
from server.sync import serializers
from server.sync.changelog import append_change
from server.sync.merge import apply_field
from server.sync.models import (
    AssignmentLocalStatus,
    UserAssignment,
    UserAssignmentOverride,
    UserCourse,
    UserCourseOverride,
    UserCourseSkippedDate,
)

courses_router = APIRouter(prefix="/courses", tags=["courses"])
assignments_router = APIRouter(prefix="/assignments", tags=["assignments"])
logger = structlog.get_logger(__name__)


class CourseOverridePut(BaseModel):
    """Field triplets: `<field>_updated_at` present == "this field was
    edited"; the value itself may be null to clear nullable fields."""

    custom_name: str | None = Field(default=None, max_length=256)
    custom_name_updated_at: datetime | None = None
    color_hex: str | None = Field(default=None, max_length=16)
    color_hex_updated_at: datetime | None = None
    is_hidden: bool | None = None
    is_hidden_updated_at: datetime | None = None


class AssignmentOverridePut(BaseModel):
    local_status: str | None = None
    local_status_updated_at: datetime | None = None
    note: str | None = None
    note_updated_at: datetime | None = None


class SkippedDatePut(BaseModel):
    reason: str | None = None


@courses_router.get("")
async def list_courses(
    auth: CurrentAuthDep,
    session: SessionDep,
    semester: str | None = Query(default=None, max_length=16),
):
    course_filter = [
        UserCourse.user_id == auth.user_id,
        UserCourse.deleted_at.is_(None),
    ]
    if semester:
        course_filter.append(UserCourse.semester == semester)
    courses = (
        (await session.execute(select(UserCourse).where(*course_filter)))
        .scalars()
        .all()
    )
    course_ids = [c.id for c in courses]
    overrides = []
    skipped = []
    if course_ids:
        overrides = (
            (
                await session.execute(
                    select(UserCourseOverride).where(
                        UserCourseOverride.user_id == auth.user_id,
                        UserCourseOverride.user_course_id.in_(course_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        skipped = (
            (
                await session.execute(
                    select(UserCourseSkippedDate).where(
                        UserCourseSkippedDate.user_id == auth.user_id,
                        UserCourseSkippedDate.user_course_id.in_(course_ids),
                        UserCourseSkippedDate.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
    return {
        "courses": [serializers.course_to_dict(c) for c in courses],
        "overrides": [serializers.course_override_to_dict(o) for o in overrides],
        "skipped_dates": [serializers.skipped_date_to_dict(s) for s in skipped],
    }


async def _get_owned_course(
    session, user_id: uuid.UUID, course_id: int
) -> UserCourse:
    course = (
        await session.execute(
            select(UserCourse).where(
                UserCourse.id == course_id,
                UserCourse.user_id == user_id,
                UserCourse.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if course is None:
        raise HTTPException(status_code=404, detail="course not found")
    return course


@courses_router.put("/{course_id}/override")
async def put_course_override(
    course_id: int,
    payload: CourseOverridePut,
    auth: CurrentAuthDep,
    session: SessionDep,
):
    await _get_owned_course(session, auth.user_id, course_id)
    override = (
        await session.execute(
            select(UserCourseOverride).where(
                UserCourseOverride.user_id == auth.user_id,
                UserCourseOverride.user_course_id == course_id,
            )
        )
    ).scalar_one_or_none()
    if override is None:
        override = UserCourseOverride(
            user_id=auth.user_id, user_course_id=course_id
        )
        session.add(override)
        await session.flush()

    now = datetime.now(UTC)
    changed_fields = []
    for field_name, value, ts in (
        ("custom_name", payload.custom_name, payload.custom_name_updated_at),
        ("color_hex", payload.color_hex, payload.color_hex_updated_at),
        ("is_hidden", payload.is_hidden, payload.is_hidden_updated_at),
    ):
        if ts is None:
            continue
        if field_name == "is_hidden" and value is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="is_hidden cannot be null",
            )
        if apply_field(
            override,
            field_name,
            value,
            client_ts=ts,
            device_id=auth.device_id,
            now=now,
        ):
            changed_fields.append(field_name)

    if changed_fields:
        await append_change(
            session,
            user_id=auth.user_id,
            entity_type="course_override",
            entity_id=str(course_id),
            operation="upsert",
            payload={"fields": changed_fields},
            device_id=auth.device_id,
        )
    return {"override": serializers.course_override_to_dict(override)}


@courses_router.put("/{course_id}/skipped-dates/{skipped_on}")
async def put_skipped_date(
    course_id: int,
    skipped_on: date,
    payload: SkippedDatePut,
    auth: CurrentAuthDep,
    session: SessionDep,
):
    await _get_owned_course(session, auth.user_id, course_id)
    row = (
        await session.execute(
            select(UserCourseSkippedDate).where(
                UserCourseSkippedDate.user_id == auth.user_id,
                UserCourseSkippedDate.user_course_id == course_id,
                UserCourseSkippedDate.skipped_on == skipped_on,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = UserCourseSkippedDate(
            user_id=auth.user_id,
            user_course_id=course_id,
            skipped_on=skipped_on,
            reason=payload.reason,
            created_by_device_id=auth.device_id,
        )
        session.add(row)
        await session.flush()
    else:
        row.reason = payload.reason if payload.reason is not None else row.reason
        row.deleted_at = None
        row.deleted_by_device_id = None

    await append_change(
        session,
        user_id=auth.user_id,
        entity_type="course_skipped_date",
        entity_id=str(row.id),
        operation="upsert",
        device_id=auth.device_id,
    )
    return serializers.skipped_date_to_dict(row)


@courses_router.delete(
    "/{course_id}/skipped-dates/{skipped_on}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_skipped_date(
    course_id: int,
    skipped_on: date,
    auth: CurrentAuthDep,
    session: SessionDep,
) -> None:
    await _get_owned_course(session, auth.user_id, course_id)
    row = (
        await session.execute(
            select(UserCourseSkippedDate).where(
                UserCourseSkippedDate.user_id == auth.user_id,
                UserCourseSkippedDate.user_course_id == course_id,
                UserCourseSkippedDate.skipped_on == skipped_on,
                UserCourseSkippedDate.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="skipped date not found")
    row.deleted_at = datetime.now(UTC)
    row.deleted_by_device_id = auth.device_id
    await append_change(
        session,
        user_id=auth.user_id,
        entity_type="course_skipped_date",
        entity_id=str(row.id),
        operation="delete",
        device_id=auth.device_id,
    )


@assignments_router.get("")
async def list_assignments(auth: CurrentAuthDep, session: SessionDep):
    assignments = (
        (
            await session.execute(
                select(UserAssignment).where(
                    UserAssignment.user_id == auth.user_id,
                    UserAssignment.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    overrides = (
        (
            await session.execute(
                select(UserAssignmentOverride).where(
                    UserAssignmentOverride.user_id == auth.user_id
                )
            )
        )
        .scalars()
        .all()
    )
    return {
        "assignments": [serializers.assignment_to_dict(a) for a in assignments],
        "overrides": [
            serializers.assignment_override_to_dict(o) for o in overrides
        ],
    }


@assignments_router.put("/{assignment_id}/override")
async def put_assignment_override(
    assignment_id: int,
    payload: AssignmentOverridePut,
    auth: CurrentAuthDep,
    session: SessionDep,
):
    assignment = (
        await session.execute(
            select(UserAssignment).where(
                UserAssignment.id == assignment_id,
                UserAssignment.user_id == auth.user_id,
                UserAssignment.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if assignment is None:
        raise HTTPException(status_code=404, detail="assignment not found")

    override = (
        await session.execute(
            select(UserAssignmentOverride).where(
                UserAssignmentOverride.user_id == auth.user_id,
                UserAssignmentOverride.user_assignment_id == assignment_id,
            )
        )
    ).scalar_one_or_none()
    if override is None:
        override = UserAssignmentOverride(
            user_id=auth.user_id, user_assignment_id=assignment_id
        )
        session.add(override)
        await session.flush()

    valid_statuses = {s.value for s in AssignmentLocalStatus}
    now = datetime.now(UTC)
    changed_fields = []
    for field_name, value, ts in (
        ("local_status", payload.local_status, payload.local_status_updated_at),
        ("note", payload.note, payload.note_updated_at),
    ):
        if ts is None:
            continue
        if field_name == "local_status" and value not in valid_statuses:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"local_status must be one of {sorted(valid_statuses)}",
            )
        if apply_field(
            override,
            field_name,
            value,
            client_ts=ts,
            device_id=auth.device_id,
            now=now,
        ):
            changed_fields.append(field_name)

    if changed_fields:
        await append_change(
            session,
            user_id=auth.user_id,
            entity_type="assignment_override",
            entity_id=str(assignment_id),
            operation="upsert",
            payload={"fields": changed_fields},
            device_id=auth.device_id,
        )
    return {"override": serializers.assignment_override_to_dict(override)}
