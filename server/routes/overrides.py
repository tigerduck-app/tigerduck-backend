"""Override endpoints: assignment local_status + course color/name/delete,
and ignoring all of a course's assignments.

Clients call these when the user swipes to mark done / ignored / delete.
The change is written to the per-user override table and appended to the
changelog so other devices pick it up via delta-sync.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import structlog
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, StrictBool, StrictInt
from sqlalchemy import delete, or_, select, update
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
    color_hex: str | None = None
    custom_name: str | None = None
    locale: str | None = None
    # Strict: a lax bool takes "yes" or 1 as true.
    ignore_assignments: StrictBool | None = None
    # The numeric Moodle course id, stored as BIGINT; the path's `moodle_id`
    # is the course's idnumber. Kept until a later request sends another.
    # Strict, or true would be stored as course 1.
    moodle_course_id: StrictInt | None = Field(default=None, ge=1, le=2**63 - 1)


class CourseOverrideResponse(BaseModel):
    id: int
    color_hex: str | None
    custom_names: dict[str, str]
    ignore_assignments: bool
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
        # Clear the mark on every row of the assignment: `/sync/full` keys a
        # mark left on a soft-deleted row once no row is live, which would
        # bring it back. A row holding a note keeps it; the rest go.
        of_assignment = (
            UserAssignmentOverride.user_id == auth.user_id,
            UserAssignmentOverride.user_assignment_id.in_(
                select(UserAssignment.id).where(
                    UserAssignment.user_id == auth.user_id,
                    UserAssignment.moodle_assignment_id == moodle_assignment_id,
                )
            ),
        )
        has_note = or_(
            UserAssignmentOverride.note.is_not(None),
            UserAssignmentOverride.note_updated_at.is_not(None),
        )
        await session.execute(
            update(UserAssignmentOverride)
            .where(*of_assignment, has_note)
            .values(
                local_status=AssignmentLocalStatus.none.value,
                local_status_updated_at=now,
                local_status_device_id=auth.device_id,
                local_status_received_at=now,
                updated_at=now,
            )
        )
        await session.execute(
            delete(UserAssignmentOverride).where(*of_assignment, ~has_note)
        )

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
            local_status_received_at=now,
        )
        .on_conflict_do_update(
            index_elements=["user_id", "user_assignment_id"],
            set_={
                "local_status": payload.local_status,
                "local_status_updated_at": now,
                "local_status_device_id": auth.device_id,
                "local_status_received_at": now,
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
    "/courses/{moodle_id}/override",
)
async def patch_course_override(
    moodle_id: str,
    payload: CourseOverrideRequest,
    auth: CurrentAuthDep,
    session: SessionDep,
):
    now = datetime.now(UTC)

    course = await _get_course_by_moodle_id(session, auth.user_id, moodle_id)

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

    if payload.ignore_assignments is not None:
        existing.ignore_assignments = payload.ignore_assignments
        existing.ignore_assignments_updated_at = now
        existing.ignore_assignments_device_id = auth.device_id
        changed_fields.append("ignore_assignments")

    if payload.moodle_course_id is not None:
        existing.moodle_course_id = payload.moodle_course_id

    existing.updated_at = now
    await session.flush()

    # Only a field other devices show sends them to a full sync; the numeric
    # id is for the server's reminder scan alone.
    if changed_fields:
        await append_change(
            session,
            user_id=auth.user_id,
            entity_type=ChangeEntityType.course_override.value,
            entity_id=str(course.id),
            operation="upsert",
            payload={"fields": changed_fields},
            device_id=auth.device_id,
        )
        await _enqueue_sync_trigger(session, auth.user_id, auth.device_id)

    from server.syncjobs.log_entries import log_sync

    parts = []
    if payload.color_hex is not None:
        parts.append(f"color={payload.color_hex}")
    if payload.custom_name is not None:
        parts.append(f"name={payload.custom_name!r}")
    if payload.ignore_assignments is not None:
        parts.append(f"ignore_assignments={payload.ignore_assignments}")
    if payload.moodle_course_id is not None:
        parts.append(f"moodle_course_id={payload.moodle_course_id}")
    await log_sync(
        session,
        user_id=auth.user_id,
        source="override",
        message=f"Course override: moodle_id={moodle_id} → {', '.join(parts)}",
        device_id=auth.device_id,
        detail={
            "moodle_id": moodle_id,
            "moodle_course_id": payload.moodle_course_id,
            "fields": changed_fields,
        },
    )

    return CourseOverrideResponse(
        id=existing.id,
        color_hex=existing.color_hex,
        custom_names=existing.custom_names or {},
        ignore_assignments=existing.ignore_assignments,
        updated_at=now.isoformat(),
    )


async def _get_assignment_by_moodle_id(
    session: AsyncSession, user_id, moodle_assignment_id: int
) -> UserAssignment:
    # Uniqueness is (user_id, moodle_course_id, moodle_assignment_id), and
    # client uploads use moodle_course_id=0 (real course ID unknown on the
    # client), so a server-synced row and an upload placeholder can both be
    # live for the same assignment until the next fetch soft-deletes the
    # placeholder. Prefer the server-synced row — overrides attached to it
    # survive that fetch.
    #
    # With no live row, a soft-deleted one takes the mark: the server's fetch
    # leaves out a course the user cannot submit to, which the apps still
    # show, and `/sync/full` keys a mark on such a row while none is live.
    row = (
        (
            await session.execute(
                select(UserAssignment)
                .where(
                    UserAssignment.moodle_assignment_id == moodle_assignment_id,
                    UserAssignment.user_id == user_id,
                )
                .order_by(
                    UserAssignment.deleted_at.is_not(None).asc(),
                    (UserAssignment.moodle_course_id == 0).asc(),
                    UserAssignment.id,
                )
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return row


async def _get_course_by_moodle_id(
    session: AsyncSession, user_id, moodle_id: str
) -> UserCourse:
    # moodle_id has no per-user unique constraint: a Moodle fetch creates a
    # shell row (course_key "moodle:<id>", semester "") while a client
    # portal upload can carry the same moodle_id on its own course_key, so
    # two live rows can share a moodle_id. Prefer the portal row — it is
    # the one the user actually sees (real semester/schedule).
    row = (
        (
            await session.execute(
                select(UserCourse)
                .where(
                    UserCourse.moodle_id == moodle_id,
                    UserCourse.user_id == user_id,
                )
                .order_by(
                    UserCourse.course_key.like("moodle:%").asc(),
                    UserCourse.id,
                )
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        # Unlike the "Not Found" a backend gives for a route it lacks, so an
        # app can tell the course is gone.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="course_not_found"
        )
    return row
