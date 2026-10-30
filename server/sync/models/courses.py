"""A user's courses: the enrolment rows, their per-user overrides,
deletion tombstones, and the dates they marked as skipped."""

from __future__ import annotations
import uuid
from datetime import date, datetime
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
from .enums import CourseSource, EnrollmentStatus


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
    # The stamp of the user's add, None for a course only the roster brought
    # (`server/sync/membership.py`).
    added_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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
    # Whether a semester reset wrote this row rather than a single-course
    # delete. Only a reset tombstone lets its own author upload the key
    # again -- see `upload_courses`. Both kinds bind every other device.
    deleted_by_reset: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa.false(), default=False
    )
    # Whether the deleted row was a course the user added by hand (`source`
    # 'user_added'). Left by a single delete, such a tombstone does not bind
    # its author's roster either, and that device's snapshot leaves it out;
    # see `upload_courses`.
    user_added: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa.false(), default=False
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
    # Each locale's name is stamped on its own, as a clock value
    # (`server/sync/merge.py`), so names edited in two locales on two devices
    # both stay. `custom_name_updated_at` holds the latest of them.
    custom_names_updated_at: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default="{}"
    )

    # One name for readers without a locale (the class reminder copy). The
    # academics PUT and the initial upload still send a locale-less
    # `custom_name`, which feeds both 'zh' and 'en' with the a1b2c3d4e5f6
    # backfill convention (`merge.set_locale_less_name`). A cleared name
    # stays as "" in both, as the override PATCH keeps a cleared locale, so
    # a snapshot carries the clear.
    @property
    def custom_name(self) -> str | None:
        names = self.custom_names or {}
        return names.get("zh") or names.get("en") or None

    color_hex: Mapped[str | None] = mapped_column(String(16), nullable=True)
    color_hex_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    color_hex_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_devices.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Every assignment of the course counts as ignored, including ones
    # Moodle adds later. The reminder scan finds the ones the server fetched
    # by `moodle_course_id`, and the apps' placeholders by course number
    # while the course's term is the current one.
    ignore_assignments: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa.false(), default=False
    )
    ignore_assignments_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ignore_assignments_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_devices.id", ondelete="SET NULL"),
        nullable=True,
    )
    # The course's numeric Moodle id, sent by the client. A course the apps
    # upload is keyed by its Moodle idnumber, but the assignments the server
    # fetches itself carry only this number, so the reminder scan matches an
    # ignored course to them by it.
    moodle_course_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (UniqueConstraint("user_id", "user_course_id"),)
class UserSemesterReset(Base):
    """The latest reset of one term's courses, by its stamp. The course
    choices of the term stamped at or before it are cleared and refused
    (`server/sync/membership.py`); a full reset is `users.courses_reset_at`."""

    __tablename__ = "user_semester_resets"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    semester: Mapped[str] = mapped_column(String(16), primary_key=True)
    reset_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
class UserCourseRoster(Base):
    """When the school's roster of one term last arrived for the user: an
    upload naming the term in `roster_terms` and listing a course of it.
    `/sync/full` asks for the current term's once this is too old."""

    __tablename__ = "user_course_rosters"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    semester: Mapped[str] = mapped_column(String(16), primary_key=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
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
