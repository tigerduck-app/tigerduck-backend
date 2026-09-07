"""Sync bookkeeping: each user's revision watermark and the change log
that incremental sync reads to answer "what moved since revision N?"."""

from __future__ import annotations
import uuid
from datetime import date, datetime
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
