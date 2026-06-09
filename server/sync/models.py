"""ORM models for the Phase-2 sync tables (data-model spec layers 3–6).

Conventions match `server/auth/models.py`: string status columns + CHECK
constraints, TIMESTAMPTZ, partial indexes. Per-field merge columns come in
triplets: `<field>`, `<field>_updated_at`, `<field>_device_id` — see
`server/sync/merge.py` for the clamp/apply logic.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from server.db import Base


class CourseSource(StrEnum):
    ntust_portal = "ntust_portal"
    user_added = "user_added"


class EnrollmentStatus(StrEnum):
    enrolled = "enrolled"
    dropped = "dropped"
    completed = "completed"


class AssignmentLocalStatus(StrEnum):
    none = "none"
    locally_completed = "locally_completed"
    ignored = "ignored"
    archived = "archived"


class SubscriptionMode(StrEnum):
    and_ = "AND"
    or_ = "OR"


class ChangeOperation(StrEnum):
    upsert = "upsert"
    delete = "delete"


class ChangeEntityType(StrEnum):
    course = "course"
    course_override = "course_override"
    course_skipped_date = "course_skipped_date"
    assignment = "assignment"
    assignment_override = "assignment_override"
    settings_document = "settings_document"
    bulletin_subscription = "bulletin_subscription"
    bulletin_state = "bulletin_state"
    bulletin_match = "bulletin_match"


SETTINGS_NAMESPACES = (
    "home_layout",
    "appearance",
    "assignment_display",
    "notification",
    "browser",
    "language",
    "schedule_display",
    "watch",
    "wearos",
)


class UserCourse(Base):
    __tablename__ = "user_courses"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    semester: Mapped[str] = mapped_column(String(16))
    course_key: Mapped[str] = mapped_column(String(128))
    course_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(
        String(32),
        default=CourseSource.ntust_portal.value,
        server_default="ntust_portal",
    )
    course_name: Mapped[str] = mapped_column(String(256))
    course_name_en: Mapped[str | None] = mapped_column(String(256), nullable=True)
    instructors: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default="{}"
    )
    credits: Mapped[float | None] = mapped_column(Numeric(3, 1), nullable=True)
    classroom: Mapped[str | None] = mapped_column(String(128), nullable=True)
    enrolled_count: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    max_count: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    moodle_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    schedule_json: Mapped[list] = mapped_column(
        JSONB, default=list, server_default="[]"
    )
    classroom_map: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default="{}"
    )
    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    enrollment_status: Mapped[str] = mapped_column(
        String(32),
        default=EnrollmentStatus.enrolled.value,
        server_default="enrolled",
    )
    fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("user_id", "semester", "course_key"),
        CheckConstraint(
            "source IN ('ntust_portal', 'user_added')", name="chk_course_source"
        ),
        CheckConstraint(
            "(source = 'ntust_portal' AND course_no IS NOT NULL)"
            " OR source = 'user_added'",
            name="chk_course_source_key",
        ),
        CheckConstraint(
            "enrollment_status IN ('enrolled', 'dropped', 'completed')",
            name="chk_enrollment_status",
        ),
        Index(
            "idx_user_courses_semester",
            "user_id",
            "semester",
            postgresql_where=sa.text("deleted_at IS NULL"),
        ),
    )


class UserCourseOverride(Base):
    __tablename__ = "user_course_overrides"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    user_course_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("user_courses.id", ondelete="CASCADE")
    )

    custom_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    custom_name_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    custom_name_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_devices.id", ondelete="SET NULL"),
        nullable=True,
    )

    color_hex: Mapped[str | None] = mapped_column(String(16), nullable=True)
    color_hex_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    color_hex_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_devices.id", ondelete="SET NULL"),
        nullable=True,
    )

    is_hidden: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sa.text("false")
    )
    is_hidden_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_hidden_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_devices.id", ondelete="SET NULL"),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (UniqueConstraint("user_id", "user_course_id"),)


class UserCourseSkippedDate(Base):
    __tablename__ = "user_course_skipped_dates"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    user_course_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("user_courses.id", ondelete="CASCADE")
    )
    skipped_on: Mapped[date] = mapped_column(Date)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_devices.id", ondelete="SET NULL"),
        nullable=True,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deleted_by_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_devices.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("user_id", "user_course_id", "skipped_on"),
        Index(
            "idx_course_skipped_dates_active",
            "user_id",
            "skipped_on",
            postgresql_where=sa.text("deleted_at IS NULL"),
        ),
    )


class UserAssignment(Base):
    __tablename__ = "user_assignments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    user_course_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("user_courses.id", ondelete="SET NULL"),
        nullable=True,
    )
    moodle_course_id: Mapped[int] = mapped_column(BigInteger)
    moodle_assignment_id: Mapped[int] = mapped_column(BigInteger)
    # Snapshot fields — survive even if the user_course mapping fails.
    course_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    course_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    title: Mapped[str] = mapped_column(String(512))
    due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cutoff_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    allow_from_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    moodle_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    intro_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_is_submitted: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sa.text("false")
    )
    provider_submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    provider_grading_status: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    provider_grade: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("user_id", "moodle_course_id", "moodle_assignment_id"),
        Index(
            "idx_user_assignments_active",
            "user_id",
            postgresql_where=sa.text("deleted_at IS NULL"),
        ),
        # Pre-filter for reminder generation only — the real exclusion must
        # also join user_assignment_overrides (spec note).
        Index(
            "idx_user_assignments_due",
            "user_id",
            "due_at",
            postgresql_where=sa.text(
                "deleted_at IS NULL AND provider_is_submitted = false"
            ),
        ),
        Index(
            "idx_user_assignments_course",
            "user_course_id",
            postgresql_where=sa.text("deleted_at IS NULL"),
        ),
    )


class UserAssignmentOverride(Base):
    __tablename__ = "user_assignment_overrides"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    user_assignment_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("user_assignments.id", ondelete="CASCADE")
    )

    local_status: Mapped[str] = mapped_column(
        String(32), default=AssignmentLocalStatus.none.value, server_default="none"
    )
    local_status_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    local_status_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_devices.id", ondelete="SET NULL"),
        nullable=True,
    )

    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    note_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    note_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_devices.id", ondelete="SET NULL"),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("user_id", "user_assignment_id"),
        CheckConstraint(
            "local_status IN ('none', 'locally_completed', 'ignored', 'archived')",
            name="chk_local_status",
        ),
    )
