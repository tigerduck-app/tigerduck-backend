"""ORM models for devices and scheduled pushes.

Design notes
------------
* `DeviceRegistration` — one row per (user_id, device_id). `pts_token_hex`
  is the Push-to-Start token reported by iOS. `device_token_hex` is the
  standard APNs device token (not used in Checkpoint 1–3; reserved for
  later standard-alert pushes).
* `CustomPushSend` — one summary row per operator custom push. Written and
  read only by the portal (raw SQL); the backend never touches it. The model
  exists so Alembic can see the table — without it, autogenerate proposes
  dropping a table the portal depends on.

`ScheduledPush` and `LiveActivityUpdateToken` used to live here. `bfa32fc`
deleted the v2 push machinery that was their only reader and writer, and the
migration that drops the two now-unreachable tables follows it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from server.db import Base

# Register the v3 identity/auth and sync tables on Base.metadata so
# create_all / alembic autogenerate see them whenever the legacy models
# are imported.
import server.auth.models  # noqa: F401, E402
import server.sync.models  # noqa: F401, E402
import server.syncjobs.models  # noqa: F401, E402


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
    # Phase 4c (review 1.8): set when this physical device is also a
    # logged-in v3 user_device (matched on client_device_id). The anonymous
    # bulletin fan-out skips linked devices — the user-level push_jobs flow
    # owns their notifications, otherwise the device would receive every
    # bulletin twice during the dual-track migration.
    linked_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        # Partial index the bulletin dispatcher's targeting query rides on.
        # Declared here so it is part of `Base.metadata`; without it Alembic
        # reads the database's copy as an index the models no longer want.
        Index(
            "ix_devices_class_enabled",
            "device_class",
            postgresql_where=sa.text("server_push_enabled = true"),
        ),
    )


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
    # v3: membership references user_devices (the persistent-UUID device
    # row), not the abandoned v2 device_registrations. See migration
    # a8c2f1e0d4b6.
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_devices.id", ondelete="CASCADE"),
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
    # Comma-joined target classes (e.g. "iphone,ipad") for `/recent` audit
    # display. Same value across every row of a request — the record path
    # keeps the equivalent in `Bulletin.dispatch_filter_json`.
    target_classes: Mapped[str] = mapped_column(
        String(64), default="", server_default=""
    )

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


class CustomPushSend(Base):
    """One row per operator custom push, powering the portal's "recent sends"
    list. The portal writes and reads it with raw SQL (see
    `portal/app/routes/custom_push.py`); nothing in `server/` uses it.

    It is mapped here purely so it exists in `Base.metadata`. Alembic diffs the
    database against that metadata, so an unmapped table reads as one the
    models no longer want and autogenerate emits `op.drop_table` for it. Keep
    this class in sync with `b2e4c6a8f0d1_custom_push_sends.py`."""

    __tablename__ = "custom_push_sends"

    request_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    # 'record' (also stored as a bulletin) or 'popup' (ephemeral).
    kind: Mapped[str] = mapped_column(String(16))
    # Comma-joined target classes, e.g. "iphone,android".
    target_classes: Mapped[str] = mapped_column(String(128))
    total: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (Index("ix_custom_push_sends_created_at", "created_at"),)
