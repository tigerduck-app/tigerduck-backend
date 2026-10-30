"""Course uploads: the first-run bulk upload and the per-course upsert
that follows it."""

from __future__ import annotations
from datetime import UTC, datetime, timedelta
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, StrictInt
from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from server.auth.dependencies import CurrentAuthDep
from server.db import SessionDep
from server.push.sync_trigger import enqueue_sync_trigger
from server.auth.models import PushDelivery, PushDeliveryStatus, PushJobStatus, User, UserDevice
from server.sync.changelog import RevisionExpired, append_change, lock_sync_state, read_changes
from server.sync.upload import InitialUploadRequest, process_initial_upload
from server.sync import membership
from server.sync.merge import OLDEST, ClockProtocolDep, apply_field, edit_time, to_clock
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
from server.syncjobs.log_entries import log_sync
from server.syncjobs.models import SyncJobType
from ._shared import _push_back_sync_jobs, _trigger_push_tick, logger

router = APIRouter(prefix="/sync", tags=["sync"])


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
    await log_sync(
        session,
        user_id=auth.user_id,
        source="sync",
        message=f"Initial upload: {result.counts}",
        device_id=auth.device_id,
        detail=result.counts,
    )
    return {
        "counts": result.counts,
        "current_revision": result.current_revision,
    }
class CourseUploadItem(BaseModel):
    # The columns' lengths, so a longer value gets a 422, not a 500 from the database.
    # A course number stops at 48, so the moodle_id an upload without one derives,
    # the term and the number, fits its 64; NTUST's are 9 or 10 characters.
    semester: str = Field(max_length=16)
    course_no: str = Field(max_length=48)
    course_name: str = Field(max_length=256)
    course_name_en: str | None = Field(default=None, max_length=256)
    moodle_id: str | None = Field(default=None, max_length=64)
    credits: float | None = None
    classroom: str | None = Field(default=None, max_length=128)
    instructors: list[str] = []
    schedule_json: dict | list = {}
    classroom_map: dict = {}
class CourseOverrideUploadItem(BaseModel):
    course_key: str
    color_hex: str | None = Field(default=None, max_length=16)
class CourseAdd(BaseModel):
    course_key: str = Field(max_length=128)
    # The add's TigerSync clock value. Strict, or true would be stamp 1.
    hlc: StrictInt = Field(ge=0, le=2**63 - 1)
class CourseUploadRequest(BaseModel):
    courses: list[CourseUploadItem]
    course_overrides: list[CourseOverrideUploadItem] = Field(
        default_factory=list, max_length=500
    )
    force_keys: list[str] = Field(
        default_factory=list, max_length=50,
        description="Course keys whose tombstones should be cleared (explicit user re-add).",
    )
    # Protocol 2's hand-adds, in place of `force_keys`: each names a course
    # in `courses` and is applied by its stamp (`sync/membership.py`).
    adds: list[CourseAdd] = Field(default_factory=list, max_length=50)
@router.post("/courses/upload")
async def upload_courses(
    payload: CourseUploadRequest,
    auth: CurrentAuthDep,
    session: SessionDep,
    request: Request,
    background_tasks: BackgroundTasks,
    speaks_clock: ClockProtocolDep,
):
    now = datetime.now(UTC)
    payload_keys = {f"client:{c.semester}:{c.course_no}" for c in payload.courses}
    if speaks_clock and not {a.course_key for a in payload.adds} <= payload_keys:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="add_not_in_courses"
        )
    adds: dict[str, datetime] = {}
    for add in payload.adds if speaks_clock else ():
        # A key sent twice keeps its latest stamp.
        stamp = edit_time(add.hlc, now)
        adds[add.course_key] = max(stamp, adds.get(add.course_key, stamp))
    # Before any row: the override writes take it first too.
    state = await lock_sync_state(session, auth.user_id)

    existing_sources = dict(
        (await session.execute(
            select(UserCourse.course_key, UserCourse.source).where(
                UserCourse.user_id == auth.user_id
            )
        )).all()
    )
    existing_keys = set(existing_sources)

    added_at: dict[str, datetime] = {}
    add_answers: list[dict] = []
    if speaks_clock:
        tombstoned_keys, added_at, add_answers, school_back = await _roster_by_stamp(
            session, auth.user_id, payload, payload_keys, adds
        )
        # An add that brings back a course deleted while it was not added by
        # hand brings back a school row, as `force_keys` does below.
        added_by_hand = set(added_at) - school_back
        back_by_hand: set[str] = set()
        # Under protocol 2 `courses` outside `adds` is the school's roster,
        # so a hand-added course it lists becomes a school row.
        listed_by_school = payload_keys - {a.course_key for a in payload.adds}
    else:
        listed_by_school: set[str] = set()
        # Clear tombstones for explicitly re-added courses (user action). A
        # course these keys bring to the server counts as added by hand, unless
        # it comes back from the deletion of one that did not. One the server
        # holds already keeps what it is, so a roster course added again by hand
        # still binds when it is hidden later, unless its key lifted the
        # tombstone of one added by hand.
        added_by_hand = set(payload.force_keys)
        back_by_hand: set[str] = set()
        if payload.force_keys:
            cleared = (await session.execute(
                delete(UserCourseTombstone)
                .where(
                    UserCourseTombstone.user_id == auth.user_id,
                    UserCourseTombstone.course_key.in_(payload.force_keys),
                )
                .returning(UserCourseTombstone.course_key, UserCourseTombstone.user_added)
            )).all()
            added_by_hand -= {t.course_key for t in cleared if not t.user_added}
            back_by_hand = {t.course_key for t in cleared if t.user_added}

        # A reset tombstone does not bind the device that wrote it. That reset
        # cleared the term precisely so this upload could replace it, and the
        # roster the portal returned seconds later is the replacement -- so
        # retire the rows it covers and let the upsert through. Anything the
        # reset dropped that this upload does not mention keeps its tombstone
        # and goes on binding every device, this one included.
        #
        # Nor does the tombstone of a course added by hand bind its author's
        # roster, an upload that forces no key. Its author's snapshot leaves it
        # out (`_read_full_snapshot`), so where the removal hid nothing on that
        # device (the iPhone's tap-again in Add course) it lists the course
        # again once its roster does, and its upload is then the one way back:
        # every other device hides the course and uploads nothing it hides, and
        # one that has not synced since would only send the copy that was
        # removed. An add's upload can carry the term as it was when that
        # course was added, a course taken back since included. An upload the
        # author put together before the removal and sent after it releases the
        # tombstone all the same; the window is that of one request.
        if auth.device_id and payload_keys:
            released = [UserCourseTombstone.deleted_by_reset.is_(True)]
            if not payload.force_keys:
                released.append(UserCourseTombstone.user_added.is_(True))
            await session.execute(
                delete(UserCourseTombstone).where(
                    UserCourseTombstone.user_id == auth.user_id,
                    or_(*released),
                    UserCourseTombstone.deleted_by_device_id == auth.device_id,
                    UserCourseTombstone.course_key.in_(payload_keys),
                )
            )

        # Tombstoned courses must not be resurrected by a bulk portal upload.
        # A removal kept beside a roster row is left out of these apps'
        # snapshot (`_released_tombstones`), so that row still updates.
        tombstoned_keys = {
            t.course_key
            for t in (await session.execute(
                select(UserCourseTombstone).where(
                    UserCourseTombstone.user_id == auth.user_id,
                )
            )).scalars()
            if not (t.user_added and not t.deleted_by_reset and t.course_key in existing_keys)
        }
        # A released app's re-add is stamped when it arrives.
        added_at = dict.fromkeys(payload.force_keys, now)

    upserted = 0
    skipped = 0
    uploaded_keys: set[str] = set()
    for c in payload.courses:
        course_key = f"client:{c.semester}:{c.course_no}"
        if course_key in tombstoned_keys:
            skipped += 1
            continue
        uploaded_keys.add(course_key)
        moodle_id = c.moodle_id or f"{c.semester}{c.course_no}"
        source = (
            CourseSource.user_added if course_key in added_by_hand else CourseSource.ntust_portal
        ).value
        stmt = (
            pg_insert(UserCourse)
            .values(
                user_id=auth.user_id,
                semester=c.semester,
                course_key=course_key,
                source=source,
                added_at=added_at.get(course_key),
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
                updated_by_device_id=auth.device_id,
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
                    "version": UserCourse.__table__.c.version + 1,
                    "updated_by_device_id": auth.device_id,
                    "source": (
                        CourseSource.user_added.value
                        if course_key in back_by_hand
                        else CourseSource.ntust_portal.value
                        if course_key in listed_by_school
                        else UserCourse.__table__.c.source
                    ),
                    "added_at": added_at.get(course_key, UserCourse.__table__.c.added_at),
                },
            )
        )
        await session.execute(stmt)
        upserted += 1

    new_keys = uploaded_keys - existing_keys
    # Under protocol 2 an add to a row the server holds changes what the
    # snapshot says of it too, and so does a hand-add the roster now lists.
    if speaks_clock:
        by_school = {
            k for k in listed_by_school if existing_sources.get(k) == CourseSource.user_added
        }
        new_keys |= (set(added_at) | by_school) & uploaded_keys
    logger.info(
        "sync.upload_courses",
        user_id=str(auth.user_id),
        device_id=str(auth.device_id) if auth.device_id else None,
        upserted=upserted,
        new_keys=sorted(new_keys) if new_keys else [],
        uploaded_keys=sorted(uploaded_keys),
    )
    if new_keys:
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
        push_row = await enqueue_sync_trigger(
            session,
            auth.user_id,
            source_device_id=str(auth.device_id) if auth.device_id else None,
        )
        if push_row:
            await log_sync(
                session, user_id=auth.user_id, source="push",
                message=f"sync_trigger push job #{push_row} queued ({len(new_keys)} new courses)",
                device_id=auth.device_id,
                detail={"push_job_id": push_row, "new_keys": sorted(new_keys)},
            )
            background_tasks.add_task(_trigger_push_tick, request)

    overrides_applied = 0
    override_items = [i for i in payload.course_overrides if i.color_hex]
    courses_by_key: dict[str, UserCourse] = {}
    overrides_by_course_id: dict[int, UserCourseOverride] = {}
    if override_items:
        courses_by_key = {
            c.course_key: c
            for c in (
                await session.execute(
                    select(UserCourse).where(
                        UserCourse.user_id == auth.user_id,
                        UserCourse.course_key.in_(
                            {i.course_key for i in override_items}
                        ),
                    )
                )
            ).scalars()
        }
        overrides_by_course_id = {
            o.user_course_id: o
            for o in (
                await session.execute(
                    select(UserCourseOverride).where(
                        UserCourseOverride.user_id == auth.user_id,
                        UserCourseOverride.user_course_id.in_(
                            [c.id for c in courses_by_key.values()]
                        ),
                    )
                )
            ).scalars()
        }
    resets = await membership.Resets.load(session, auth.user_id) if override_items else None
    for item in override_items:
        course = courses_by_key.get(item.course_key)
        if course is None:
            continue
        # The fill counts as the oldest value, so a course the server had
        # refuses it under its term's reset; one this upload adds does not.
        if item.course_key in existing_keys and resets.covering(course.semester) is not None:
            continue
        override = overrides_by_course_id.get(course.id)
        if override is None:
            override = UserCourseOverride(
                user_id=auth.user_id, user_course_id=course.id
            )
            session.add(override)
            await session.flush()
            overrides_by_course_id[course.id] = override
        # The device's colour, picked or assigned, fills a course without one
        # as the oldest value: a colour the user set or cleared outranks it.
        if override.color_hex is None and apply_field(
            override,
            "color_hex",
            item.color_hex,
            client_ts=OLDEST,
            device_id=auth.device_id,
            now=now,
        ):
            overrides_applied += 1
            logger.debug(
                "sync.course_color_set",
                course_key=item.course_key,
                color_hex=item.color_hex,
                device_id=str(auth.device_id) if auth.device_id else None,
            )
        elif override.color_hex != item.color_hex:
            logger.debug(
                "sync.course_color_skipped",
                course_key=item.course_key,
                server_hex=override.color_hex,
                client_hex=item.color_hex,
            )

    course_nos = [c.course_no for c in payload.courses]
    await log_sync(
        session,
        user_id=auth.user_id,
        source="sync",
        message=f"Courses uploaded: {upserted} upserted, {len(new_keys)} new, {skipped} tombstoned, {overrides_applied} colors set",
        device_id=auth.device_id,
        detail={
            "upserted": upserted,
            "skipped_tombstoned": skipped,
            "new_keys": sorted(new_keys) if new_keys else [],
            "overrides_applied": overrides_applied,
            "course_nos": course_nos,
        },
    )
    await _push_back_sync_jobs(session, auth.user_id, SyncJobType.ntust_courses.value)
    if auth.device_id:
        device = await session.get(UserDevice, auth.device_id)
        if device:
            device.last_seen_at = datetime.now(UTC)
    await session.commit()
    return {
        "upserted": upserted,
        "skipped_tombstoned": skipped,
        "overrides_applied": overrides_applied,
        # What actually landed. A 200 with `skipped_tombstoned` > 0 is not
        # "the server has seen these courses", and a client that records
        # it as such retires a hand-typed course's protection on the
        # strength of an upload the server refused.
        "accepted_keys": sorted(uploaded_keys),
        **({"adds": add_answers} if speaks_clock else {}),
    }


async def _roster_by_stamp(session, user_id, payload, payload_keys, adds):
    """Protocol 2's membership rules for an upload (`sync/membership.py`).

    An add wins when later than its key's add, tombstone and covering
    resets, and lifts the tombstone. The roster is base data: it skips a
    hidden key, lifts a reset tombstone, and shows a removed key beside its
    removal tombstone, whose stamp keeps refusing older edits. Answers the
    keys to skip, the winning adds' stamps, one answer per add and the
    winning adds that lift a tombstone not `user_added`. `adds` holds each
    add's stamp. The caller holds the user's sync lock.
    """
    rows = {
        r.course_key: r
        for r in (
            await session.execute(
                select(UserCourse).where(
                    UserCourse.user_id == user_id, UserCourse.course_key.in_(payload_keys)
                )
            )
        ).scalars()
    }
    tombstones = {
        t.course_key: t
        for t in (
            await session.execute(
                select(UserCourseTombstone).where(
                    UserCourseTombstone.user_id == user_id,
                    UserCourseTombstone.course_key.in_(payload_keys),
                )
            )
        ).scalars()
    }
    resets = await membership.Resets.load(session, user_id)
    skip: set[str] = set()
    lifted: set[str] = set()
    won: dict[str, datetime] = {}
    school_back: set[str] = set()
    answers: list[dict] = []
    for c in payload.courses:
        key = f"client:{c.semester}:{c.course_no}"
        tombstone = tombstones.get(key)
        if key in adds:
            stored = membership.membership_stamp(rows.get(key), tombstone, resets, c.semester)
            applied = stored is None or adds[key] > stored
            if applied:
                won[key] = adds[key]
                lifted.add(key)
                if tombstone is not None and not tombstone.user_added:
                    school_back.add(key)
            else:
                skip.add(key)
            winner = adds[key] if applied else stored
            answers.append({"course_key": key, "applied": applied, "hlc": to_clock(winner)})
        elif tombstone is None:
            continue
        elif membership.tombstone_kind(tombstone) == membership.HIDE:
            skip.add(key)
        elif membership.tombstone_kind(tombstone) == membership.RESET:
            lifted.add(key)
    if lifted:
        await session.execute(
            delete(UserCourseTombstone).where(
                UserCourseTombstone.user_id == user_id,
                UserCourseTombstone.course_key.in_(lifted),
            )
        )
    return skip, won, answers, school_back
