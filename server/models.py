"""ORM models for devices and scheduled pushes.

Design notes
------------
* `DeviceRegistration` — one row per (user_id, device_id). `pts_token_hex`
  is the Push-to-Start token reported by iOS. `device_token_hex` is the
  standard APNs device token (not used in Checkpoint 1–3; reserved for
  later standard-alert pushes).
* `ScheduledPush` — future push instructions. `push_id` is deterministic
  (`{device_id}:{source_id}:{scenario}`) so client sync can UPSERT without
  creating duplicates. `payload_json` holds the full LiveActivitySnapshot
  JSON — dispatcher just reads and ships it.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from server.db import Base


class PushStatus(StrEnum):
    pending = "pending"
    sent = "sent"
    failed = "failed"
    cancelled = "cancelled"


class LiveActivityTokenStatus(StrEnum):
    active = "active"
    ended = "ended"
    failed = "failed"
    cancelled = "cancelled"


class DevicePlatform(StrEnum):
    """Push delivery platform. Picks which sender the dispatcher uses —
    APNs for apple, FCM for android. Stored as a plain string (not a PG
    ENUM) for the same reason we do this with every other enum-ish column:
    trivial to add a value later via default-only migration, no ALTER TYPE
    gymnastics."""

    apple = "apple"
    android = "android"


class DeviceRegistration(Base):
    __tablename__ = "device_registrations"

    device_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    platform: Mapped[str] = mapped_column(
        String(16), default=DevicePlatform.apple.value, server_default="apple"
    )
    pts_token_hex: Mapped[str] = mapped_column(String(512))
    device_token_hex: Mapped[str | None] = mapped_column(String(512), nullable=True)
    bundle_id: Mapped[str] = mapped_column(String(128))
    attrs_type: Mapped[str] = mapped_column(String(128))
    apns_env: Mapped[str] = mapped_column(String(16))
    device_class: Mapped[str] = mapped_column(
        String(16), default="", server_default=""
    )
    server_push_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=sa.text("true")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    pushes: Mapped[list["ScheduledPush"]] = relationship(
        back_populates="device", cascade="all, delete-orphan"
    )
    live_activity_tokens: Mapped[list["LiveActivityUpdateToken"]] = relationship(
        back_populates="device", cascade="all, delete-orphan"
    )


class ScheduledPush(Base):
    __tablename__ = "scheduled_pushes"

    push_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    device_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("device_registrations.device_id", ondelete="CASCADE"),
        index=True,
    )
    source_id: Mapped[str] = mapped_column(String(128))
    scenario: Mapped[str] = mapped_column(String(32))
    fire_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload_json: Mapped[dict] = mapped_column(JSONB)

    status: Mapped[str] = mapped_column(String(16), default=PushStatus.pending.value)
    attempts: Mapped[int] = mapped_column(BigInteger, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    device: Mapped[DeviceRegistration] = relationship(back_populates="pushes")

    __table_args__ = (
        Index("ix_pushes_due", "status", "fire_at"),
    )


class LiveActivityUpdateToken(Base):
    __tablename__ = "live_activity_update_tokens"

    activity_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    device_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("device_registrations.device_id", ondelete="CASCADE"),
        index=True,
    )
    source_id: Mapped[str] = mapped_column(String(128))
    scenario: Mapped[str] = mapped_column(String(32))
    update_token_hex: Mapped[str] = mapped_column(String(512))
    countdown_target: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
    snapshot_json: Mapped[dict] = mapped_column(JSONB)

    status: Mapped[str] = mapped_column(
        String(16), default=LiveActivityTokenStatus.active.value
    )
    attempts: Mapped[int] = mapped_column(BigInteger, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    device: Mapped[DeviceRegistration] = relationship(
        back_populates="live_activity_tokens"
    )

    __table_args__ = (
        Index("ix_live_activity_tokens_due", "status", "countdown_target"),
    )


def build_push_id(device_id: str, source_id: str, scenario: str) -> str:
    return f"{device_id}:{source_id}:{scenario}"


class DeviceList(Base):
    """Operator-managed named bucket of devices.

    Lets the custom-push UI target an ad-hoc cohort (e.g. "beta-android",
    "spring-2026-pilot") without having to re-enter device IDs every time.
    A device may live in any number of lists; the `device_list_members`
    join table is the source of truth for membership.
    """

    __tablename__ = "device_lists"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    members: Mapped[list["DeviceListMember"]] = relationship(
        back_populates="list", cascade="all, delete-orphan"
    )


class DeviceListMember(Base):
    __tablename__ = "device_list_members"

    list_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("device_lists.id", ondelete="CASCADE"),
        primary_key=True,
    )
    device_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("device_registrations.device_id", ondelete="CASCADE"),
        primary_key=True,
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    list: Mapped[DeviceList] = relationship(back_populates="members")

    # Reverse-direction lookups ("which lists is this device in?") drive
    # the row-level "Add to list" dropdown on the portal devices page —
    # without this index that's a seqscan on every page render.
    __table_args__ = (
        Index("ix_device_list_members_device_id", "device_id"),
    )


class CustomPushStatus(StrEnum):
    pending = "pending"
    sent = "sent"
    failed = "failed"
    cancelled = "cancelled"


class CustomPushDispatch(Base):
    """One row per (request, device) for pure-notification custom pushes.

    Record-keeping pushes do NOT use this table — they live in `bulletins`
    with `source='custom_push'` and `dispatch_filter_json` set.
    """

    __tablename__ = "custom_push_dispatches"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(32), index=True)
    device_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("device_registrations.device_id", ondelete="CASCADE"),
        index=True,
    )
    title: Mapped[str] = mapped_column(String(500))
    body: Mapped[str] = mapped_column(Text)
    force_ring: Mapped[bool] = mapped_column(Boolean, default=True)
    notification_id: Mapped[str] = mapped_column(String(64))

    status: Mapped[str] = mapped_column(
        String(16), default=CustomPushStatus.pending.value
    )
    attempts: Mapped[int] = mapped_column(BigInteger, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index(
            "ix_custom_push_pending",
            "status",
            postgresql_where=(status == "pending"),
        ),
    )
