"""Course deletion. Both endpoints write tombstones rather than removing
rows, so other devices learn about the deletion on their next sync."""

from __future__ import annotations
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, status
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from server.auth.dependencies import CurrentAuthDep
from server.db import SessionDep
from server.auth.models import PushDelivery, PushDeliveryStatus, PushJob, PushJobStatus, User, UserDevice
from server.sync.changelog import RevisionExpired, append_change, lock_sync_state, read_changes
from server.sync.models import (
    ChangeEntityType,
    CourseSource,
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
from server.sync import membership
from server.sync.merge import ClockProtocolDep, edit_time, require_stamp, to_clock
from server.sync.models import UserSemesterReset
from server.syncjobs.log_entries import log_sync
from ._shared import _trigger_push_tick, logger

router = APIRouter(prefix="/sync", tags=["sync"])

# The request's TigerSync clock value.
StampQuery = Annotated[int | None, Query(ge=0, le=2**63 - 1)]


async def _queue_sync_trigger(session, auth, now, request, background_tasks) -> int | None:
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
        background_tasks.add_task(_trigger_push_tick, request)
    return push_row


@router.delete("/courses")
async def delete_all_courses(
    auth: CurrentAuthDep,
    session: SessionDep,
    request: Request,
    background_tasks: BackgroundTasks,
    speaks_clock: ClockProtocolDep,
    semester: str | None = Query(default=None, max_length=16),
    hlc: StampQuery = None,
):
    """Wipe the user's courses — every semester, or only `semester` when
    given. Used by 'reset course timetable'. Under protocol 2 the reset
    clears only what is stamped at or before it (`_reset_by_stamp`)."""
    require_stamp(speaks_clock, hlc)
    if speaks_clock:
        now = datetime.now(UTC)
        return await _reset_by_stamp(
            session, auth, semester, edit_time(hlc, now), now, request, background_tasks
        )
    if semester and auth.device_id is None:
        # A semester reset writes tombstones that bind every device but
        # their author, and the author is known by device id. Without one
        # the tombstones would bind this session too, and its next upload
        # could never release them.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="device_id_required"
        )
    now = datetime.now(UTC)
    # Before any row: the override writes take it first too.
    state = await lock_sync_state(session, auth.user_id)
    # Recorded for the apps that compare stamps, which refuse what predates it.
    reset_at = await _record_reset(session, auth.user_id, semester, edit_time(hlc, now))

    course_scope = [UserCourse.user_id == auth.user_id]
    tombstone_scope = [UserCourseTombstone.user_id == auth.user_id]
    if semester:
        course_scope.append(UserCourse.semester == semester)
        tombstone_scope.append(UserCourseTombstone.semester == semester)

    rows = (await session.execute(
        delete(UserCourse)
        .where(*course_scope)
        .returning(
            UserCourse.id, UserCourse.course_key, UserCourse.semester, UserCourse.course_no,
            UserCourse.source,
        )
    )).all()

    # Replace the tombstones in scope rather than merely clearing them.
    #
    # Clearing was enough to unblock this device's immediate re-upload, and
    # it still is -- a reset tombstone does not bind its own author. What it
    # was not enough for is every *other* device: with nothing on record, a
    # phone that had not yet reconciled would push its pre-reset roster back
    # on its next refresh, `upload_courses` had no reason to refuse it, and
    # the term the user had just reset filled straight back up.
    #
    # The clear runs first and unconditionally so the rewrite also covers a
    # user who deleted every course one by one and then reset: those older
    # single-delete tombstones would otherwise keep binding this device.
    #
    # Only a semester reset writes them. A full reset already propagates
    # through `courses_reset_at` below, which tells every device to drop its
    # course overlay wholesale; tombstoning on top of that would put a
    # permanent marker on every course of every past term, and no refresh
    # re-uploads a term the student finished years ago -- their timetables
    # would stay blank for good, on the resetting device too.
    if semester is None:
        # A full reset propagates through `courses_reset_at` below, which
        # tells every device to drop its course overlay wholesale, so no
        # tombstone is needed and every existing one goes.
        await session.execute(delete(UserCourseTombstone).where(*tombstone_scope))
    else:
        # A semester reset is the deletion of record for everything the
        # term held, and it binds every device but its author, who is
        # known by device id (see `upload_courses`).
        #
        # Every tombstone the term already carries becomes a reset
        # tombstone of this device's: a single delete bound this device
        # too, and the roster the portal returns next is allowed to bring
        # that course back — but only through this device's upload, which
        # releases exactly the keys it names. Dropping those tombstones
        # instead let a stale device push the whole old roster back the
        # moment a user who had deleted every course by hand pressed
        # Reset; and clearing reset tombstones was what a second tap did
        # while the first was still refetching, with the same result.
        await session.execute(
            update(UserCourseTombstone)
            .where(*tombstone_scope)
            .values(
                deleted_at=now,
                deleted_by_device_id=auth.device_id,
                deleted_by_reset=True,
            )
        )
        if rows:
            tombstones = pg_insert(UserCourseTombstone).values([
                {
                    "user_id": auth.user_id,
                    "course_key": row.course_key,
                    "semester": row.semester,
                    "course_no": row.course_no,
                    "deleted_at": now,
                    "deleted_by_device_id": auth.device_id,
                    "deleted_by_reset": True,
                    "user_added": row.source == CourseSource.user_added,
                }
                for row in rows
            ])
            await session.execute(
                tombstones.on_conflict_do_update(
                    index_elements=["user_id", "course_key"],
                    set_={
                        "deleted_at": now,
                        "deleted_by_device_id": auth.device_id,
                        "deleted_by_reset": True,
                        "user_added": tombstones.excluded.user_added,
                    },
                )
            )

    if not rows:
        return {"deleted": 0, "reset_hlc": to_clock(reset_at)}

    for row in rows:
        await append_change(
            session,
            user_id=auth.user_id,
            entity_type=ChangeEntityType.course.value,
            entity_id=str(row.id),
            operation="delete",
            payload={"course_key": row.course_key},
            device_id=auth.device_id,
            locked_state=state,
        )
    push_row = await _queue_sync_trigger(session, auth, now, request, background_tasks)

    deleted_keys = [r.course_key for r in rows]
    push_status = f"push job #{push_row} queued" if push_row else "push deduplicated"
    await log_sync(
        session,
        user_id=auth.user_id,
        source="sync",
        message=f"{'Semester ' + semester if semester else 'All'} courses deleted: {len(rows)} removed — {push_status}",
        device_id=auth.device_id,
        detail={"deleted": len(rows), "semester": semester, "course_keys": deleted_keys},
    )
    logger.info(
        "sync.delete_all_courses",
        user_id=str(auth.user_id),
        semester=semester,
        deleted=len(rows),
    )
    return {"deleted": len(rows), "reset_hlc": to_clock(reset_at)}


async def _record_reset(session, user_id, semester: str | None, stamp: datetime) -> datetime:
    """Store the reset's stamp, never moving a stored one back."""
    if semester is None:
        stored = await session.scalar(select(User.courses_reset_at).where(User.id == user_id))
        recorded = membership.latest(stored, stamp)
        await session.execute(
            update(User).where(User.id == user_id).values(courses_reset_at=recorded)
        )
        return recorded
    insert = pg_insert(UserSemesterReset).values(
        user_id=user_id, semester=semester, reset_at=stamp
    )
    return (
        await session.execute(
            insert.on_conflict_do_update(
                index_elements=["user_id", "semester"],
                set_={
                    "reset_at": func.greatest(UserSemesterReset.reset_at, insert.excluded.reset_at)
                },
            ).returning(UserSemesterReset.reset_at)
        )
    ).scalar_one()


async def _reset_by_stamp(session, auth, semester, stamp, now, request, background_tasks):
    """A reset wins when later than the one it would replace, and clears the
    term's choices stamped at or before it (`sync/membership.py`). A row
    with a later add or override value stays; the others go, each leaving
    a reset tombstone after a term reset so released apps still obey it."""
    state = await lock_sync_state(session, auth.user_id)
    resets = await membership.Resets.load(session, auth.user_id)
    stored = resets.covering(semester) if semester else resets.full
    if stored is not None and stamp <= stored:
        return {"deleted": 0, "applied": False, "reset_hlc": to_clock(stored)}
    await _record_reset(session, auth.user_id, semester, stamp)

    course_scope = [UserCourse.user_id == auth.user_id]
    tombstone_scope = [UserCourseTombstone.user_id == auth.user_id]
    if semester:
        course_scope.append(UserCourse.semester == semester)
        tombstone_scope.append(UserCourseTombstone.semester == semester)
    await session.execute(
        delete(UserCourseTombstone).where(
            *tombstone_scope, UserCourseTombstone.deleted_at <= stamp
        )
    )
    rows = (await session.execute(select(UserCourse).where(*course_scope))).scalars().all()
    overrides = {
        o.user_course_id: o
        for o in (
            await session.execute(
                select(UserCourseOverride).where(
                    UserCourseOverride.user_id == auth.user_id,
                    UserCourseOverride.user_course_id.in_([r.id for r in rows]),
                )
            )
        ).scalars()
    }
    gone = [
        r for r in rows
        if not (r.added_at is not None and r.added_at > stamp)
        and not membership.override_outlasts(overrides.get(r.id), stamp)
    ]
    gone_ids = {r.id for r in gone}
    cleared = [
        r for r in rows
        if r.id not in gone_ids and membership.clear_before(r, overrides.get(r.id), stamp)
    ]
    if gone:
        await session.execute(delete(UserCourse).where(UserCourse.id.in_(gone_ids)))
    if gone and semester:
        # A later tombstone of the key stayed, and already keeps the course out.
        later = set(
            (
                await session.execute(
                    select(UserCourseTombstone.course_key).where(*tombstone_scope)
                )
            ).scalars()
        )
        values = [
            {
                "user_id": auth.user_id,
                "course_key": r.course_key,
                "semester": r.semester,
                "course_no": r.course_no,
                "deleted_at": stamp,
                "deleted_by_device_id": auth.device_id,
                "deleted_by_reset": True,
                "user_added": r.source == CourseSource.user_added,
            }
            for r in gone
            if r.course_key not in later
        ]
        if values:
            await session.execute(pg_insert(UserCourseTombstone).values(values))

    changes = [(r.id, "delete", r.course_key) for r in gone]
    changes += [(r.id, "upsert", r.course_key) for r in cleared]
    for entity_id, operation, course_key in changes or [(semester or "*", "upsert", None)]:
        await append_change(
            session,
            user_id=auth.user_id,
            entity_type=ChangeEntityType.course.value,
            entity_id=str(entity_id),
            operation=operation,
            payload={"course_key": course_key} if course_key else {"reset": semester},
            device_id=auth.device_id,
            locked_state=state,
        )
    push_row = await _queue_sync_trigger(session, auth, now, request, background_tasks)
    await log_sync(
        session,
        user_id=auth.user_id,
        source="sync",
        message=(
            f"{'Semester ' + semester if semester else 'All'} courses reset:"
            f" {len(gone)} removed, {len(cleared)} kept with later edits"
            f" — {'push job #' + str(push_row) + ' queued' if push_row else 'push deduplicated'}"
        ),
        device_id=auth.device_id,
        detail={
            "deleted": len(gone),
            "kept": len(rows) - len(gone),
            "semester": semester,
            "course_keys": [r.course_key for r in gone],
        },
    )
    return {"deleted": len(gone), "applied": True, "reset_hlc": to_clock(stamp)}


@router.delete("/courses/{course_key:path}")
async def delete_course(
    course_key: str,
    auth: CurrentAuthDep,
    session: SessionDep,
    request: Request,
    background_tasks: BackgroundTasks,
    speaks_clock: ClockProtocolDep,
    hlc: StampQuery = None,
    kind: Annotated[Literal["hide", "remove"] | None, Query()] = None,
):
    """Hide a course, or remove one added by hand, when the stamp is later
    than the key's add, tombstone and covering resets (`sync/membership.py`).
    Under protocol 2 the tombstone is written even for a course the server
    lacks, so a hide of a course never uploaded still binds."""
    require_stamp(speaks_clock, hlc)
    now = datetime.now(UTC)
    stamp = edit_time(hlc, now)
    # Before any row: the override writes take it first too.
    state = await lock_sync_state(session, auth.user_id)
    row = await session.scalar(
        select(UserCourse).where(
            UserCourse.user_id == auth.user_id, UserCourse.course_key == course_key
        )
    )
    tombstone = await session.scalar(
        select(UserCourseTombstone).where(
            UserCourseTombstone.user_id == auth.user_id,
            UserCourseTombstone.course_key == course_key,
        )
    )
    semester = (
        row.semester if row else tombstone.semester if tombstone
        else membership.semester_of(course_key)
    )
    resets = await membership.Resets.load(session, auth.user_id)
    stored = membership.membership_stamp(row, tombstone, resets, semester)
    # A released app's delete of a course the server lacks changes nothing.
    if (
        (row is None and not speaks_clock)
        or semester is None
        or (stored is not None and stamp <= stored)
    ):
        return {"deleted": 0, "applied": False, "hlc": to_clock(stored) if stored else None}

    if kind is None:
        by_hand = row is not None and row.source == CourseSource.user_added
        kind = membership.REMOVE if by_hand else membership.HIDE
    restamp_only = (
        row is None and tombstone is not None and membership.tombstone_kind(tombstone) == kind
    )
    course_no = (
        row.course_no if row else tombstone.course_no if tombstone
        else course_key.rpartition(":")[2]
    )
    if row is not None:
        await session.execute(delete(UserCourse).where(UserCourse.id == row.id))
    marker = {
        "deleted_at": stamp,
        "deleted_by_device_id": auth.device_id,
        "deleted_by_reset": False,
        "user_added": kind == membership.REMOVE,
    }
    await session.execute(
        pg_insert(UserCourseTombstone)
        .values(
            user_id=auth.user_id,
            course_key=course_key,
            semester=semester,
            course_no=course_no,
            **marker,
        )
        .on_conflict_do_update(index_elements=["user_id", "course_key"], set_=marker)
    )
    answer = {"deleted": 1 if row else 0, "applied": True, "hlc": to_clock(stamp)}
    if restamp_only:
        return answer

    await append_change(
        session,
        user_id=auth.user_id,
        entity_type=ChangeEntityType.course.value,
        entity_id=str(row.id) if row else course_key,
        operation="delete",
        payload={"course_key": course_key},
        device_id=auth.device_id,
        locked_state=state,
    )
    push_row = await _queue_sync_trigger(session, auth, now, request, background_tasks)
    push_status = f"push job #{push_row} queued" if push_row else "push deduplicated"
    await log_sync(
        session,
        user_id=auth.user_id,
        source="sync",
        message=f"Course deleted ({kind}): {course_key} — {push_status}",
        device_id=auth.device_id,
        detail={
            "course_key": course_key,
            "course_no": course_no,
            "semester": semester,
            "kind": kind,
            "push_job_id": push_row,
            "push_deduplicated": push_row is None,
        },
    )
    return answer
