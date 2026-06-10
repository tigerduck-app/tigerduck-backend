"""Initial upload: first-login import of a device's local data.

Everything is idempotent on natural keys (semester+course_key, moodle ids,
namespace, subscription content) so a client that crashes mid-upload can
simply re-send the whole body. Server-resident data wins ties: an entity
that already exists is left untouched (another device uploaded first; this
device should pull via /v3/sync instead).

Per-field override values DO merge (via `server/sync/merge.py`) because
they carry their own client timestamps.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal

import structlog
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.sync.changelog import append_change, lock_sync_state
from server.sync.merge import apply_field
from server.sync.models import (
    UserAssignment,
    UserAssignmentOverride,
    UserBulletinSubscription,
    UserCourse,
    UserCourseOverride,
    UserCourseSkippedDate,
    UserSettingsDocument,
)

logger = structlog.get_logger(__name__)


class UploadCourse(BaseModel):
    semester: str = Field(max_length=16)
    course_key: str = Field(max_length=128)
    course_no: str | None = Field(default=None, max_length=64)
    source: Literal["ntust_portal", "user_added"] = "ntust_portal"
    course_name: str = Field(max_length=256)
    course_name_en: str | None = Field(default=None, max_length=256)
    instructors: list[str] = Field(default_factory=list)
    credits: float | None = None
    classroom: str | None = Field(default=None, max_length=128)
    enrolled_count: int | None = None
    max_count: int | None = None
    moodle_id: str | None = Field(default=None, max_length=64)
    schedule_json: list = Field(default_factory=list)
    classroom_map: dict = Field(default_factory=dict)
    enrollment_status: Literal["enrolled", "dropped", "completed"] = "enrolled"

    @model_validator(mode="after")
    def portal_requires_course_no(self) -> "UploadCourse":
        if self.source == "ntust_portal" and not self.course_no:
            raise ValueError("ntust_portal courses require course_no")
        return self


class UploadCourseOverride(BaseModel):
    semester: str
    course_key: str
    custom_name: str | None = None
    custom_name_updated_at: datetime | None = None
    color_hex: str | None = Field(default=None, max_length=16)
    color_hex_updated_at: datetime | None = None
    is_hidden: bool | None = None
    is_hidden_updated_at: datetime | None = None


class UploadSkippedDate(BaseModel):
    semester: str
    course_key: str
    skipped_on: date
    reason: str | None = None


class UploadAssignment(BaseModel):
    moodle_course_id: int
    moodle_assignment_id: int
    course_no: str | None = None
    course_name: str | None = None
    title: str = Field(max_length=512)
    due_at: datetime | None = None
    cutoff_at: datetime | None = None
    allow_from_at: datetime | None = None
    moodle_url: str | None = Field(default=None, max_length=512)
    intro_html: str | None = None
    provider_is_submitted: bool = False
    provider_submitted_at: datetime | None = None


class UploadAssignmentOverride(BaseModel):
    moodle_course_id: int
    moodle_assignment_id: int
    local_status: (
        Literal["none", "locally_completed", "ignored", "archived"] | None
    ) = None
    local_status_updated_at: datetime | None = None
    note: str | None = None
    note_updated_at: datetime | None = None


class UploadSettingsDocument(BaseModel):
    namespace: Literal[
        "home_layout",
        "appearance",
        "assignment_display",
        "notification",
        "browser",
        "language",
        "schedule_display",
        "watch",
        "wearos",
    ]
    schema_version: int = Field(default=1, ge=1)
    document: dict


class UploadSubscription(BaseModel):
    name: str | None = Field(default=None, max_length=128)
    orgs: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    mode: Literal["AND", "OR"] = "AND"
    enabled: bool = True


class InitialUploadRequest(BaseModel):
    # Bounded lists: the upload runs inside the per-user FOR UPDATE sync
    # lock with a flush per entity — an unbounded body would let one JWT
    # hold that lock (and a DB connection) for minutes. Limits sit an
    # order of magnitude above any real student's data.
    courses: list[UploadCourse] = Field(default_factory=list, max_length=500)
    course_overrides: list[UploadCourseOverride] = Field(
        default_factory=list, max_length=500
    )
    course_skipped_dates: list[UploadSkippedDate] = Field(
        default_factory=list, max_length=2000
    )
    assignments: list[UploadAssignment] = Field(
        default_factory=list, max_length=5000
    )
    assignment_overrides: list[UploadAssignmentOverride] = Field(
        default_factory=list, max_length=5000
    )
    settings_documents: list[UploadSettingsDocument] = Field(
        default_factory=list, max_length=50
    )
    bulletin_subscriptions: list[UploadSubscription] = Field(
        default_factory=list, max_length=200
    )


@dataclass
class UploadResult:
    counts: dict[str, int]
    current_revision: int


async def process_initial_upload(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    device_id: uuid.UUID | None,
    payload: InitialUploadRequest,
) -> UploadResult:
    now = datetime.now(UTC)
    counts = {
        "courses": 0,
        "course_overrides": 0,
        "course_skipped_dates": 0,
        "assignments": 0,
        "assignment_overrides": 0,
        "settings_documents": 0,
        "bulletin_subscriptions": 0,
    }

    # Lock the user's sync-state row once for the whole upload — appending
    # per entity would re-acquire the same lock hundreds of times.
    locked_state = await lock_sync_state(session, user_id)

    async def log(entity_type: str, entity_id: str, hint: dict | None = None):
        await append_change(
            session,
            user_id=user_id,
            entity_type=entity_type,
            entity_id=entity_id,
            operation="upsert",
            payload=hint,
            device_id=device_id,
            locked_state=locked_state,
        )

    # --- Courses (skip existing: server copy is authoritative) ---
    course_by_key: dict[tuple[str, str], UserCourse] = {}
    existing_courses = (
        (
            await session.execute(
                select(UserCourse).where(UserCourse.user_id == user_id)
            )
        )
        .scalars()
        .all()
    )
    for course in existing_courses:
        course_by_key[(course.semester, course.course_key)] = course

    for item in payload.courses:
        key = (item.semester, item.course_key)
        if key in course_by_key:
            continue
        course = UserCourse(
            user_id=user_id,
            semester=item.semester,
            course_key=item.course_key,
            course_no=item.course_no,
            source=item.source,
            course_name=item.course_name,
            course_name_en=item.course_name_en,
            instructors=item.instructors,
            credits=item.credits,
            classroom=item.classroom,
            enrolled_count=item.enrolled_count,
            max_count=item.max_count,
            moodle_id=item.moodle_id,
            schedule_json=item.schedule_json,
            classroom_map=item.classroom_map,
            enrollment_status=item.enrollment_status,
        )
        session.add(course)
        await session.flush()
        course_by_key[key] = course
        counts["courses"] += 1
        await log("course", str(course.id))

    # --- Course overrides (per-field merge) ---
    for item in payload.course_overrides:
        course = course_by_key.get((item.semester, item.course_key))
        if course is None:
            logger.info(
                "sync.upload.override_skipped_unknown_course",
                course_key=item.course_key,
            )
            continue
        override = (
            await session.execute(
                select(UserCourseOverride).where(
                    UserCourseOverride.user_id == user_id,
                    UserCourseOverride.user_course_id == course.id,
                )
            )
        ).scalar_one_or_none()
        if override is None:
            override = UserCourseOverride(user_id=user_id, user_course_id=course.id)
            session.add(override)
            await session.flush()
        changed = False
        for field_name, value, ts in (
            ("custom_name", item.custom_name, item.custom_name_updated_at),
            ("color_hex", item.color_hex, item.color_hex_updated_at),
            ("is_hidden", item.is_hidden, item.is_hidden_updated_at),
        ):
            if value is None:
                continue
            changed |= apply_field(
                override,
                field_name,
                value,
                client_ts=ts or now,
                device_id=device_id,
                now=now,
            )
        if changed:
            counts["course_overrides"] += 1
            await log("course_override", str(course.id))

    # --- Skipped dates (independent entities) ---
    for item in payload.course_skipped_dates:
        course = course_by_key.get((item.semester, item.course_key))
        if course is None:
            continue
        existing = (
            await session.execute(
                select(UserCourseSkippedDate).where(
                    UserCourseSkippedDate.user_id == user_id,
                    UserCourseSkippedDate.user_course_id == course.id,
                    UserCourseSkippedDate.skipped_on == item.skipped_on,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue
        skipped = UserCourseSkippedDate(
            user_id=user_id,
            user_course_id=course.id,
            skipped_on=item.skipped_on,
            reason=item.reason,
            created_by_device_id=device_id,
        )
        session.add(skipped)
        await session.flush()
        counts["course_skipped_dates"] += 1
        await log("course_skipped_date", str(skipped.id))

    # --- Assignments (snapshot; skip existing) ---
    assignment_by_key: dict[tuple[int, int], UserAssignment] = {}
    existing_assignments = (
        (
            await session.execute(
                select(UserAssignment).where(UserAssignment.user_id == user_id)
            )
        )
        .scalars()
        .all()
    )
    for assignment in existing_assignments:
        assignment_by_key[
            (assignment.moodle_course_id, assignment.moodle_assignment_id)
        ] = assignment

    for item in payload.assignments:
        key = (item.moodle_course_id, item.moodle_assignment_id)
        if key in assignment_by_key:
            continue
        assignment = UserAssignment(
            user_id=user_id,
            moodle_course_id=item.moodle_course_id,
            moodle_assignment_id=item.moodle_assignment_id,
            course_no=item.course_no,
            course_name=item.course_name,
            title=item.title,
            due_at=item.due_at,
            cutoff_at=item.cutoff_at,
            allow_from_at=item.allow_from_at,
            moodle_url=item.moodle_url,
            intro_html=item.intro_html,
            provider_is_submitted=item.provider_is_submitted,
            provider_submitted_at=item.provider_submitted_at,
        )
        session.add(assignment)
        await session.flush()
        assignment_by_key[key] = assignment
        counts["assignments"] += 1
        await log("assignment", str(assignment.id))

    # --- Assignment overrides (per-field merge) ---
    for item in payload.assignment_overrides:
        assignment = assignment_by_key.get(
            (item.moodle_course_id, item.moodle_assignment_id)
        )
        if assignment is None:
            continue
        override = (
            await session.execute(
                select(UserAssignmentOverride).where(
                    UserAssignmentOverride.user_id == user_id,
                    UserAssignmentOverride.user_assignment_id == assignment.id,
                )
            )
        ).scalar_one_or_none()
        if override is None:
            override = UserAssignmentOverride(
                user_id=user_id, user_assignment_id=assignment.id
            )
            session.add(override)
            await session.flush()
        changed = False
        for field_name, value, ts in (
            ("local_status", item.local_status, item.local_status_updated_at),
            ("note", item.note, item.note_updated_at),
        ):
            if value is None:
                continue
            changed |= apply_field(
                override,
                field_name,
                value,
                client_ts=ts or now,
                device_id=device_id,
                now=now,
            )
        if changed:
            counts["assignment_overrides"] += 1
            await log("assignment_override", str(assignment.id))

    # --- Settings documents (first write wins; later devices pull) ---
    for item in payload.settings_documents:
        existing = (
            await session.execute(
                select(UserSettingsDocument).where(
                    UserSettingsDocument.user_id == user_id,
                    UserSettingsDocument.namespace == item.namespace,
                    UserSettingsDocument.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue
        doc = UserSettingsDocument(
            user_id=user_id,
            namespace=item.namespace,
            schema_version=item.schema_version,
            document=item.document,
            created_by_device_id=device_id,
            updated_by_device_id=device_id,
        )
        session.add(doc)
        await session.flush()
        counts["settings_documents"] += 1
        await log("settings_document", item.namespace, {"revision": doc.revision})

    # --- Bulletin subscriptions (content-deduped) ---
    existing_subs = (
        (
            await session.execute(
                select(UserBulletinSubscription).where(
                    UserBulletinSubscription.user_id == user_id,
                    UserBulletinSubscription.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    existing_rules = {
        (tuple(sorted(s.orgs or [])), tuple(sorted(s.tags or [])), s.mode)
        for s in existing_subs
    }
    for item in payload.bulletin_subscriptions:
        rule = (tuple(sorted(item.orgs)), tuple(sorted(item.tags)), item.mode)
        if rule in existing_rules:
            continue
        sub = UserBulletinSubscription(
            user_id=user_id,
            name=item.name,
            orgs=item.orgs,
            tags=item.tags,
            mode=item.mode,
            enabled=item.enabled,
            created_by_device_id=device_id,
            updated_by_device_id=device_id,
        )
        session.add(sub)
        await session.flush()
        existing_rules.add(rule)
        counts["bulletin_subscriptions"] += 1
        await log("bulletin_subscription", str(sub.id))

    current_revision = locked_state.current_revision
    logger.info(
        "sync.initial_upload",
        user_id=str(user_id),
        **{f"count_{k}": v for k, v in counts.items()},
    )
    return UploadResult(counts=counts, current_revision=current_revision)
