"""Per-device bulletin subscriptions, per-user read state, and the matcher
runs that decide which bulletins reach which user."""

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
from .enums import SubscriptionMode


class UserBulletinSubscription(Base):
    __tablename__ = "user_bulletin_subscriptions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    # The device these rules belong to. Subscriptions are per device and
    # not part of TigerSync: each device keeps its own rules, and a matched
    # bulletin is pushed only to the devices whose rules hit it
    # (server/bulletins/user_dispatch.py).
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user_devices.id", ondelete="CASCADE")
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
        Index(
            "idx_bulletin_subs_device_active",
            "device_id",
            postgresql_where=sa.text("deleted_at IS NULL"),
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
