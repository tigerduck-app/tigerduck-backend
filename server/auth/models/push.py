"""Outbound push work: one job per intended notification, one delivery
row per device it was actually sent to."""

from __future__ import annotations
import uuid
from datetime import UTC, datetime
import sqlalchemy as sa
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from server.db import Base
from .enums import PushDeliveryStatus, PushJobStatus

#: The statuses under which a push job's `(user_id, dedupe_key)` is taken:
#: the predicate of `ux_push_jobs_dedupe_active` below. An `ON CONFLICT`
#: aimed at that partial index has to repeat it as `index_where`, or
#: Postgres cannot match the index.
PUSH_JOB_DEDUPE_ACTIVE_STATUSES: tuple[str, ...] = (
    PushJobStatus.pending.value,
    PushJobStatus.processing.value,
    PushJobStatus.sent.value,
    PushJobStatus.partial_failed.value,
)


class PushJob(Base):
    """One logical notification to a user (fan-out happens in
    push_deliveries). Created Phase 1, activated Phase 4."""

    __tablename__ = "push_jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_devices.id", ondelete="SET NULL"),
        nullable=True,
    )
    dedupe_key: Mapped[str] = mapped_column(String(256))
    channel: Mapped[str] = mapped_column(String(32))
    scenario: Mapped[str] = mapped_column(String(64))
    fire_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Client-side default, not just server_default: the claim in
    # push/pipeline.py compares this column against datetime.now(UTC), so
    # a value stamped by the database's own now() can read as not-yet-
    # available when the database clock leads the app host's -- the same
    # clock the claim later uses. server_default stays as a floor for any
    # row written outside SQLAlchemy's insert path.
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    priority: Mapped[int] = mapped_column(Integer, default=100, server_default="100")
    payload: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(
        String(16), default=PushJobStatus.pending.value, server_default="pending"
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, server_default="3")
    locked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    locked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "channel IN ('assignment', 'course', 'bulletin', 'system', 'custom', 'schedule')",
            name="chk_push_job_channel",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'sent', "
            "'partial_failed', 'failed', 'cancelled')",
            name="chk_push_job_status",
        ),
        CheckConstraint(
            "attempts >= 0 AND max_attempts > 0", name="chk_push_job_attempts"
        ),
        # The spec's original partial index only
        # covered pending/processing, which let the next 8-hour sync round
        # re-create an already-sent reminder. Covering delivered states too
        # makes the dedupe key durable; "notify again because content
        # changed" must use a NEW dedupe_key (e.g. embed due_at) after
        # cancelling the old job.
        Index(
            "ux_push_jobs_dedupe_active",
            "user_id",
            "dedupe_key",
            unique=True,
            postgresql_where=sa.text(
                "status IN ('pending', 'processing', 'sent', 'partial_failed')"
            ),
        ),
        Index(
            "idx_push_jobs_due",
            "status",
            "fire_at",
            "available_at",
            "priority",
            postgresql_where=sa.text("status = 'pending'"),
        ),
    )
class PushDelivery(Base):
    """One push_job sent to one specific token. Provider / token columns are
    denormalized so history survives token-row deletion."""

    __tablename__ = "push_deliveries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    push_job_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("push_jobs.id", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_devices.id", ondelete="SET NULL"),
        nullable=True,
    )
    push_token_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("device_push_tokens.id", ondelete="SET NULL"),
        nullable=True,
    )
    provider: Mapped[str] = mapped_column(String(16))
    token_kind: Mapped[str] = mapped_column(String(32))
    token_hash: Mapped[str] = mapped_column(String(64))
    scope_key: Mapped[str] = mapped_column(String(160), default="", server_default="")
    status: Mapped[str] = mapped_column(
        String(16), default=PushDeliveryStatus.pending.value, server_default="pending"
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, server_default="3")
    provider_message_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_retry_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'sent', 'failed', 'skipped')",
            name="chk_delivery_status",
        ),
        CheckConstraint(
            "provider IN ('apns', 'fcm')", name="chk_delivery_provider"
        ),
        CheckConstraint(
            "token_kind IN ('standard', 'push_to_start', 'live_activity_update')",
            name="chk_delivery_token_kind",
        ),
        CheckConstraint(
            "attempts >= 0 AND max_attempts > 0", name="chk_delivery_attempts"
        ),
        Index(
            "ux_push_delivery_job_token",
            "push_job_id",
            "token_hash",
            "token_kind",
            "scope_key",
            unique=True,
        ),
        Index(
            "idx_push_deliveries_pending",
            "status",
            "next_retry_at",
            postgresql_where=sa.text("status = 'pending'"),
        ),
    )
