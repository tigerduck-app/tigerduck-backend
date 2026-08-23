"""Assignments and the done/ignored marks a user puts on them."""

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
from .enums import AssignmentLocalStatus


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
