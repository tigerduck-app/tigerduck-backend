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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    updated_by_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_devices.id", ondelete="SET NULL"),
        nullable=True,
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
        ),
    )


class UserCourseTombstone(Base):
    __tablename__ = "user_course_tombstones"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    course_key: Mapped[str] = mapped_column(String(128))
    semester: Mapped[str] = mapped_column(String(16))
    course_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deleted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    deleted_by_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_devices.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        UniqueConstraint("user_id", "course_key"),
        Index("ix_course_tombstones_user_deleted", "user_id", "deleted_at"),
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

    custom_names: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    custom_name_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    custom_name_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_devices.id", ondelete="SET NULL"),
        nullable=True,
    )

    # The merge layer (apply_field in academics PUT and initial upload)
    # still speaks a singular, locale-less `custom_name`. Bridge it onto
    # the locale-keyed map with the a1b2c3d4e5f6 backfill convention: one
    # locale-less name feeds both 'zh' and 'en', the only keys the apps
    # and portal read. Without this bridge, setattr lands on a transient
    # instance attribute and the name is silently dropped.
    @property
    def custom_name(self) -> str | None:
        names = self.custom_names or {}
        return names.get("zh") or names.get("en")

    @custom_name.setter
    def custom_name(self, value: str | None) -> None:
        names = dict(self.custom_names or {})
        if value:
            names["zh"] = value
            names["en"] = value
        else:
            names.pop("zh", None)
            names.pop("en", None)
        self.custom_names = names

    color_hex: Mapped[str | None] = mapped_column(String(16), nullable=True)
    color_hex_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    color_hex_device_id: Mapped[uuid.UUID | None] = mapped_column(
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


class UserSettingsDocument(Base):
    """One JSONB settings document per (user, namespace). Revision is
    server-incremented; clients submit base_revision for optimistic
    concurrency (sync-and-push spec §2/§6)."""

    __tablename__ = "user_settings_documents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    namespace: Mapped[str] = mapped_column(String(64))
    schema_version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1"
    )
    document: Mapped[dict] = mapped_column(JSONB)
    revision: Mapped[int] = mapped_column(BigInteger, default=1, server_default="1")
    created_by_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_devices.id", ondelete="SET NULL"),
        nullable=True,
    )
    updated_by_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_devices.id", ondelete="SET NULL"),
        nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "namespace IN ("
            "'home_layout', 'appearance', 'assignment_display', "
            "'notification', 'browser', 'language', "
            "'schedule_display', 'watch', 'wearos')",
            name="chk_settings_namespace",
        ),
        CheckConstraint("revision >= 1", name="chk_settings_revision"),
        CheckConstraint(
            "schema_version >= 1", name="chk_settings_schema_version"
        ),
        Index(
            "ux_user_settings_namespace_active",
            "user_id",
            "namespace",
            unique=True,
            postgresql_where=sa.text("deleted_at IS NULL"),
        ),
        Index(
            "idx_user_settings_user_updated",
            "user_id",
            "updated_at",
            postgresql_where=sa.text("deleted_at IS NULL"),
        ),
    )


class UserBulletinSubscription(Base):
    __tablename__ = "user_bulletin_subscriptions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    orgs: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default="{}"
    )
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default="{}"
    )
    mode: Mapped[str] = mapped_column(
        String(8), default=SubscriptionMode.and_.value, server_default="AND"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=sa.text("true")
    )
    revision: Mapped[int] = mapped_column(BigInteger, default=1, server_default="1")
    created_by_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_devices.id", ondelete="SET NULL"),
        nullable=True,
    )
    updated_by_device_id: Mapped[uuid.UUID | None] = mapped_column(
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
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint("mode IN ('AND', 'OR')", name="chk_subscription_mode"),
        CheckConstraint("revision >= 1", name="chk_subscription_revision"),
        Index(
            "idx_bulletin_subs_user_active",
            "user_id",
            postgresql_where=sa.text("enabled = true AND deleted_at IS NULL"),
        ),
    )


class UserBulletinState(Base):
    """Per-field merged read/starred/hidden flags for one (user, bulletin)."""

    __tablename__ = "user_bulletin_states"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    bulletin_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("bulletins.id", ondelete="CASCADE")
    )

    is_read: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sa.text("false")
    )
    read_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    read_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_devices.id", ondelete="SET NULL"),
        nullable=True,
    )
    first_read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    is_starred: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sa.text("false")
    )
    starred_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    starred_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_devices.id", ondelete="SET NULL"),
        nullable=True,
    )

    is_hidden: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sa.text("false")
    )
    hidden_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    hidden_device_id: Mapped[uuid.UUID | None] = mapped_column(
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
        UniqueConstraint("user_id", "bulletin_id"),
        Index(
            "idx_bulletin_states_starred",
            "user_id",
            "starred_updated_at",
            postgresql_where=sa.text("is_starred = true"),
        ),
        Index(
            "idx_bulletin_states_hidden",
            "user_id",
            "hidden_updated_at",
            postgresql_where=sa.text("is_hidden = true"),
        ),
    )


class BulletinUserMatch(Base):
    """A bulletin matched one of a user's subscription rules. Created in
    Phase 2 for the unread query; the push columns are filled by the
    Phase-4 user-level dispatch flow."""

    __tablename__ = "bulletin_user_matches"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    bulletin_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("bulletins.id", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    subscription_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("user_bulletin_subscriptions.id", ondelete="SET NULL"),
        nullable=True,
    )
    match_reason: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    push_job_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("push_jobs.id", ondelete="SET NULL"),
        nullable=True,
    )
    pushed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    matched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("bulletin_id", "user_id"),
        Index(
            "idx_bulletin_matches_user", "user_id", sa.text("matched_at DESC")
        ),
        Index(
            "idx_bulletin_matches_pending_push",
            "pushed_at",
            postgresql_where=sa.text("pushed_at IS NULL"),
        ),
    )


class BulletinUserMatchRun(Base):
    """Phase-4c cursor: one row once the user-level subscription matcher
    has run for a bulletin. `bulletins.notified_at` is reserved for the
    anonymous flow, so the user-level dispatcher needs its own done-marker
    to keep the per-tick scan bounded."""

    __tablename__ = "bulletin_user_match_runs"

    bulletin_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("bulletins.id", ondelete="CASCADE"),
        primary_key=True,
        autoincrement=False,
    )
    matched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class UserSyncState(Base):
    """Per-user sync cursor. The row doubles as the per-user write lock for
    changelog appends (`server/sync/changelog.py`) — locking it inside the
    append transaction makes per-user commit order equal revision order, so
    incremental readers can never permanently skip a revision."""

    __tablename__ = "user_sync_state"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    current_revision: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0"
    )
    compacted_revision: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "current_revision >= compacted_revision", name="chk_user_sync_revision"
        ),
    )


class UserChangeLog(Base):
    """Append-only change feed. Payloads are routing hints only — never
    documents, HTML, or anything sensitive (data-model spec §6)."""

    __tablename__ = "user_change_log"

    revision: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    entity_type: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[str] = mapped_column(String(128))
    operation: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_devices.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "operation IN ('upsert', 'delete')", name="chk_changelog_operation"
        ),
        CheckConstraint(
            "entity_type IN ("
            "'course', 'course_override', 'course_skipped_date', "
            "'assignment', 'assignment_override', "
            "'settings_document', "
            "'bulletin_subscription', 'bulletin_state', 'bulletin_match')",
            name="chk_changelog_entity_type",
        ),
        Index("idx_change_log_user_revision", "user_id", "revision"),
        # Retention deletes by age — without this the daily purge seqscans.
        Index("idx_change_log_created_at", "created_at"),
    )
