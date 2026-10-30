"""Override endpoints: assignment local_status + course color/name/delete,
and ignoring all of a course's assignments.

Clients call these when the user swipes to mark done / ignored / delete.
Each field takes the edit with the latest TigerSync stamp (`hlc`, or the
arrival time for an app that sends none; `server/sync/merge.py`), compared
and written under the user's sync lock. A change is written to the per-user
override table and appended to the changelog so other devices pick it up via
delta-sync; an edit that changes no value is neither logged nor pushed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

import structlog
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, StrictBool, StrictInt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.dependencies import CurrentAuthDep
from server.db import SessionDep
from server.push.sync_trigger import enqueue_sync_trigger
from server.sync import membership, serializers
from server.sync.changelog import append_change, lock_sync_state
from server.sync.merge import (
    ClockProtocolDep,
    apply_course_name,
    apply_field,
    edit_time,
    require_stamp,
)
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
    await enqueue_sync_trigger(
        session, user_id, source_device_id=str(device_id) if device_id else None
    )


# The edit's TigerSync clock value. Strict, or true would be stamp 1.
Stamp = Annotated[StrictInt | None, Field(default=None, ge=0, le=2**63 - 1)]


class AssignmentOverrideRequest(BaseModel):
    local_status: Literal["none", "locally_completed", "ignored", "archived"]
    hlc: Stamp = None


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
    hlc: Stamp = None


@router.patch(
    "/assignments/{moodle_assignment_id}/override",
)
async def patch_assignment_override(
    moodle_assignment_id: int,
    payload: AssignmentOverrideRequest,
    auth: CurrentAuthDep,
    session: SessionDep,
    speaks_clock: ClockProtocolDep,
):
    require_stamp(speaks_clock, payload.hlc)
    now = datetime.now(UTC)
    stamp = edit_time(payload.hlc, now)
    state = await lock_sync_state(session, auth.user_id)
    assignment = await _get_assignment_by_moodle_id(
        session, auth.user_id, moodle_assignment_id
    )
    clearing = payload.local_status == AssignmentLocalStatus.none.value

    # A clear goes to every row of the assignment: `/sync/full` keys a mark
    # left on a soft-deleted row once no row is live, which would bring it
    # back. It is kept as a stamped `none` on each, so a mark made before
    # the clear and sent after it to any row loses. A mark goes to the row
    # the apps see.
    row_ids = [assignment.id]
    if clearing:
        row_ids = (
            await session.execute(
                select(UserAssignment.id).where(
                    UserAssignment.user_id == auth.user_id,
                    UserAssignment.moodle_assignment_id == moodle_assignment_id,
                )
            )
        ).scalars().all()
    overrides = {
        o.user_assignment_id: o
        for o in (
            await session.execute(
                select(UserAssignmentOverride).where(
                    UserAssignmentOverride.user_id == auth.user_id,
                    UserAssignmentOverride.user_assignment_id.in_(row_ids),
                )
            )
        ).scalars()
    }
    for row_id in row_ids:
        if row_id not in overrides:
            overrides[row_id] = UserAssignmentOverride(
                user_id=auth.user_id,
                user_assignment_id=row_id,
                local_status=AssignmentLocalStatus.none.value,
            )
            session.add(overrides[row_id])
    target = overrides[assignment.id]

    changed = False
    for override in overrides.values():
        if apply_field(
            override,
            "local_status",
            payload.local_status,
            client_ts=stamp,
            device_id=auth.device_id,
            now=now,
        ):
            changed = True
            override.local_status_received_at = now
            override.updated_at = now
    await session.flush()
    applied = target.local_status == payload.local_status

    # From the row the apps see: a clear it refused may still have cleared
    # an older mark on another row of the assignment.
    if changed:
        await append_change(
            session,
            user_id=auth.user_id,
            entity_type=ChangeEntityType.assignment_override.value,
            entity_id=str(assignment.id),
            operation="delete" if target.local_status == AssignmentLocalStatus.none else "upsert",
            payload={"local_status": target.local_status},
            device_id=auth.device_id,
            locked_state=state,
        )
        await _enqueue_sync_trigger(session, auth.user_id, auth.device_id)

    from server.syncjobs.log_entries import log_sync

    action = "deleted" if clearing else f"→ {payload.local_status}"
    kept = "" if applied else f" (kept {target.local_status}, stamped later)"
    await log_sync(
        session,
        user_id=auth.user_id,
        source="override",
        message=f"Assignment override {action}: moodle_id={moodle_assignment_id}{kept}",
        device_id=auth.device_id,
        detail={
            "moodle_assignment_id": moodle_assignment_id,
            "local_status": payload.local_status,
            "applied": applied,
        },
    )

    # `deleted` and `id` / `updated_at` are what released apps were sent.
    answer = {
        "local_status": target.local_status,
        "local_status_hlc": serializers.hlc(target.local_status_updated_at),
        "applied": applied,
    }
    if clearing:
        return {"deleted": True, **answer}
    return {"id": target.id, "updated_at": now.isoformat(), **answer}


@router.patch(
    "/courses/{moodle_id}/override",
)
async def patch_course_override(
    moodle_id: str,
    payload: CourseOverrideRequest,
    auth: CurrentAuthDep,
    session: SessionDep,
    speaks_clock: ClockProtocolDep,
):
    names = bool(payload.locale) and payload.custom_name is not None
    if payload.color_hex is not None or names or payload.ignore_assignments is not None:
        require_stamp(speaks_clock, payload.hlc)
    now = datetime.now(UTC)
    stamp = edit_time(payload.hlc, now)
    state = await lock_sync_state(session, auth.user_id)
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
            user_id=auth.user_id,
            user_course_id=course.id,
            custom_names={},
            custom_names_updated_at={},
            ignore_assignments=False,
        )
        session.add(existing)

    edit = {"client_ts": stamp, "device_id": auth.device_id, "now": now}
    changed_fields: list[str] = []
    sent: dict[str, bool] = {}

    # A reset of the course's term cleared every choice stamped before it.
    reset_at = (await membership.Resets.load(session, auth.user_id)).covering(course.semester)
    if reset_at is not None and stamp <= reset_at:
        given = {
            "color_hex": payload.color_hex is not None,
            "custom_name": names,
            "ignore_assignments": payload.ignore_assignments is not None,
        }
        sent = {name: False for name, was_sent in given.items() if was_sent}
    else:
        if payload.color_hex is not None:
            if apply_field(existing, "color_hex", payload.color_hex, **edit):
                changed_fields.append("color_hex")
            sent["color_hex"] = existing.color_hex == payload.color_hex

        if names:
            # A cleared locale stays as "": the apps apply only the locales a
            # snapshot lists, so one left out would keep the old name elsewhere.
            if apply_course_name(existing, payload.locale, payload.custom_name, **edit):
                changed_fields.append("custom_name")
            sent["custom_name"] = (existing.custom_names or {}).get(
                payload.locale
            ) == payload.custom_name

        if payload.ignore_assignments is not None:
            if apply_field(existing, "ignore_assignments", payload.ignore_assignments, **edit):
                changed_fields.append("ignore_assignments")
            sent["ignore_assignments"] = existing.ignore_assignments == payload.ignore_assignments

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
            locked_state=state,
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
    kept = sorted(name for name, applied in sent.items() if not applied)
    await log_sync(
        session,
        user_id=auth.user_id,
        source="override",
        message=(
            f"Course override: moodle_id={moodle_id} → {', '.join(parts)}"
            + (f" (kept {', '.join(kept)}, stamped later)" if kept else "")
        ),
        device_id=auth.device_id,
        detail={
            "moodle_id": moodle_id,
            "moodle_course_id": payload.moodle_course_id,
            "fields": changed_fields,
            "kept": kept,
        },
    )

    return {
        "id": existing.id,
        "color_hex": existing.color_hex,
        "custom_names": existing.custom_names or {},
        "ignore_assignments": existing.ignore_assignments,
        "updated_at": now.isoformat(),
        "color_hex_hlc": serializers.hlc(existing.color_hex_updated_at),
        "custom_names_hlc": dict(existing.custom_names_updated_at or {}),
        "ignore_assignments_hlc": serializers.hlc(existing.ignore_assignments_updated_at),
        "applied": sorted(name for name, applied in sent.items() if applied),
    }


async def _get_assignment_by_moodle_id(
    session: AsyncSession, user_id, moodle_assignment_id: int
) -> UserAssignment:
    # An assignment has one row, but the placeholder merge (migration
    # d899fe7d1f79) left some soft-deleted upload placeholders (course id 0)
    # beside the server-synced row, holding a mark. Prefer the server's row.
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
