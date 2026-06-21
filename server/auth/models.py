"""ORM models for the Phase-1 identity / auth / push tables.

Design notes
------------
* Mirrors `docs/superpowers/specs/2026-06-10-backend-data-model.md` layers 1–2.
* Status-ish columns are plain strings + CHECK constraints (same convention
  as `server/models.py`: adding a value later is a default-only migration,
  no `ALTER TYPE` gymnastics). StrEnum classes document the legal values.
* `push_jobs` / `push_deliveries` are created in Phase 1 but stay unused
  until Phase 4 (migration plan). The dedupe index deliberately also covers
  terminal "delivered" states — see `ux_push_jobs_dedupe_active` below.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

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


class UserStatus(StrEnum):
    active = "active"
    suspended = "suspended"
    pending_deletion = "pending_deletion"


class CredentialStatus(StrEnum):
    active = "active"
    expired = "expired"
    invalid = "invalid"
    revoked = "revoked"


class UserDevicePlatform(StrEnum):
    ios = "ios"
    ipados = "ipados"
    macos = "macos"
    windows = "windows"
    watchos = "watchos"
    wearos = "wearos"
    android = "android"
    web = "web"


class SessionRevokedReason(StrEnum):
    logout = "logout"
    rotated = "rotated"
    reuse_detected = "reuse_detected"
    expired = "expired"
    admin_revoked = "admin_revoked"
    credential_revoked = "credential_revoked"


class PushTokenProvider(StrEnum):
    apns = "apns"
    fcm = "fcm"


class PushTokenKind(StrEnum):
    standard = "standard"
    push_to_start = "push_to_start"
    live_activity_update = "live_activity_update"


class PushTokenStatus(StrEnum):
    active = "active"
    invalidated = "invalidated"
    expired = "expired"


class PushJobStatus(StrEnum):
    pending = "pending"
    processing = "processing"
    sent = "sent"
    partial_failed = "partial_failed"
    failed = "failed"
    cancelled = "cancelled"


class PushDeliveryStatus(StrEnum):
    pending = "pending"
    sent = "sent"
    failed = "failed"
    skipped = "skipped"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
        default=uuid.uuid4,
    )
    # Denormalized cache of the primary NTUST account; authoritative source
    # is external_accounts(provider='ntust_sso', external_user_id).
    student_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), default=UserStatus.active.value, server_default="active"
    )
    locale: Mapped[str | None] = mapped_column(
        String(16), default="zh-Hant", server_default="zh-Hant"
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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
    courses_reset_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    external_accounts: Mapped[list["ExternalAccount"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    devices: Mapped[list["UserDevice"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'suspended', 'pending_deletion')",
            name="chk_users_status",
        ),
        Index(
            "ux_users_student_id_active",
            "student_id",
            unique=True,
            postgresql_where=sa.text(
                "student_id IS NOT NULL AND deleted_at IS NULL"
            ),
        ),
    )


class ExternalAccount(Base):
    __tablename__ = "external_accounts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    provider: Mapped[str] = mapped_column(String(32))
    external_user_id: Mapped[str] = mapped_column(String(128))
    credential_status: Mapped[str] = mapped_column(
        String(32), default=CredentialStatus.active.value, server_default="active"
    )
    last_auth_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_auth_failure_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_auth_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped[User] = relationship(back_populates="external_accounts")
    credential: Mapped["ExternalAccountCredential | None"] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("provider", "external_user_id"),
        UniqueConstraint("user_id", "provider"),
        CheckConstraint(
            "credential_status IN ('active', 'expired', 'invalid', 'revoked')",
            name="chk_credential_status",
        ),
    )


class ExternalAccountCredential(Base):
    """Envelope-encrypted credential blob. DB stores ciphertext only; the
    AES keys live in env/KMS (`settings.credential_keys`)."""

    __tablename__ = "external_account_credentials"

    external_account_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("external_accounts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    encryption_algorithm: Mapped[str] = mapped_column(
        String(32), default="AES-256-GCM", server_default="AES-256-GCM"
    )
    encryption_key_id: Mapped[str] = mapped_column(String(64))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    nonce: Mapped[bytes] = mapped_column(LargeBinary)
    aad: Mapped[str] = mapped_column(String(256))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    rotated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    account: Mapped[ExternalAccount] = relationship(back_populates="credential")


class UserDevice(Base):
    __tablename__ = "user_devices"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    client_device_id: Mapped[str] = mapped_column(String(128))
    platform: Mapped[str] = mapped_column(String(16))
    device_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    app_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    os_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    server_push_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=sa.text("true")
    )
    sync_courses: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=sa.text("true")
    )
    sync_course_colors: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=sa.text("true")
    )
    sync_course_names: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=sa.text("true")
    )
    sync_assignments: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=sa.text("true")
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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

    user: Mapped[User] = relationship(back_populates="devices")
    push_tokens: Mapped[list["DevicePushToken"]] = relationship(
        back_populates="device", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("user_id", "client_device_id"),
        CheckConstraint(
            "platform IN ('ios', 'ipados', 'macos', 'windows', "
            "'watchos', 'wearos', 'android', 'web')",
            name="chk_device_platform",
        ),
    )


class AuthSession(Base):
    """One refresh-token lineage entry. Access tokens are stateless JWTs;
    refresh tokens are stored only as HMAC-SHA-256 hashes."""

    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_devices.id", ondelete="SET NULL"),
        nullable=True,
    )
    refresh_token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    replaced_by_session_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("auth_sessions.id", ondelete="SET NULL"),
        nullable=True,
    )
    reuse_detected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("expires_at > issued_at", name="chk_session_time"),
        CheckConstraint(
            "revoked_reason IS NULL OR revoked_reason IN ("
            "'logout', 'rotated', 'reuse_detected', "
            "'expired', 'admin_revoked', 'credential_revoked')",
            name="chk_revoked_reason",
        ),
        Index(
            "idx_auth_sessions_user_active",
            "user_id",
            postgresql_where=sa.text("revoked_at IS NULL"),
        ),
    )


class DevicePushToken(Base):
    __tablename__ = "device_push_tokens"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user_devices.id", ondelete="CASCADE")
    )
    provider: Mapped[str] = mapped_column(String(16))
    token_kind: Mapped[str] = mapped_column(String(32))
    # SHA-256 of the raw token (uniqueness lookups); raw value is kept for
    # actual delivery. Possession of a push token alone cannot send a push
    # (still needs our APNs/FCM credentials), so plaintext is acceptable.
    token_hash: Mapped[str] = mapped_column(String(64))
    token_value: Mapped[str] = mapped_column(String(512))
    bundle_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    topic: Mapped[str | None] = mapped_column(String(160), nullable=True)
    environment: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # What the token is bound to (e.g. "assignment:12345" for a Live
    # Activity update token). Empty string for standard / push-to-start.
    scope_key: Mapped[str] = mapped_column(String(160), default="", server_default="")
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(16), default=PushTokenStatus.active.value, server_default="active"
    )
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_failure_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    device: Mapped[UserDevice] = relationship(back_populates="push_tokens")

    __table_args__ = (
        CheckConstraint(
            "provider IN ('apns', 'fcm')", name="chk_push_token_provider"
        ),
        CheckConstraint(
            "token_kind IN ('standard', 'push_to_start', 'live_activity_update')",
            name="chk_push_token_kind",
        ),
        CheckConstraint(
            "status IN ('active', 'invalidated', 'expired')",
            name="chk_push_token_status",
        ),
        CheckConstraint(
            "environment IS NULL OR environment IN ('development', 'production')",
            name="chk_push_token_env",
        ),
        Index(
            "ux_push_token_active",
            "provider",
            "token_kind",
            "token_hash",
            "scope_key",
            unique=True,
            postgresql_where=sa.text("status = 'active'"),
        ),
        Index("idx_push_tokens_device_active", "device_id", "token_kind", "status"),
        Index(
            "idx_push_tokens_expiry",
            "status",
            "expires_at",
            postgresql_where=sa.text("status = 'active'"),
        ),
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
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
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
        # Security-review fix 1.2: the spec's original partial index only
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
