"""A user's devices, their login sessions, and their push tokens."""

from __future__ import annotations
import uuid
from datetime import datetime
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
from .enums import PushTokenStatus
# User and UserDevice reference each other, so a runtime import here
# would be a cycle. Both references are annotations, and with
# `from __future__ import annotations` SQLAlchemy resolves the target
# through its class registry rather than this module's globals — so
# the import is only needed by type checkers.
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .user import User


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
    # Form factor, for operator targeting. `platform` already separates
    # ios / ipados / macos, but every Android device — phone or tablet —
    # reports platform "android", so without this column an operator could
    # address an iPad and not an Android tablet. Mirrors
    # `device_registrations.device_class`; empty means a client that
    # registered before the column existed, and the targeting query falls
    # back to `platform` for those.
    device_class: Mapped[str] = mapped_column(
        String(16), default="", server_default=""
    )
    device_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Hardware model as the device reports it: "Google Pixel 8" on Android,
    # the machine identifier ("iPhone17,3", "Mac15,3") on Apple. For support
    # work in the portal only; nothing targets or gates on it.
    device_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    app_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    os_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # BCP-47 tag reported by the device at registration. Nullable: rows
    # predating this column, and clients that have not shipped the field
    # yet, fall back to English at send time. Never gated on a preference —
    # gating would leave a window where a device is registered but has no
    # language, exactly when the first push may need one. A device fact
    # like `app_version` / `os_version` above, not a user preference.
    locale: Mapped[str | None] = mapped_column(String(35), nullable=True)
    server_push_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=sa.text("true")
    )
    # Gates ONLY the bulletin channel, in push/pipeline.py::_materialize.
    # Separate from `server_push_enabled` above, which today reaches
    # nothing but operator custom-push targeting
    # (custom_push_targeting.py, portal/routes/custom_push.py) -- bulletins
    # are part of the always-on essential-info sync (spec §6), and this is
    # the per-device control the bulletins page itself owns.
    bulletin_push_enabled: Mapped[bool] = mapped_column(
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
    cloud_sync_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=sa.text("true")
    )
    # Device-level "does this device push THIS category to the cloud".
    # Separate from the user-level values in the `notification` settings
    # document: the document says how long before a deadline to remind,
    # these say whether this particular device takes part at all.
    sync_assignment_reminders: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=sa.text("true")
    )
    sync_live_activity: Mapped[bool] = mapped_column(
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

    user: Mapped["User"] = relationship(back_populates="devices")
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
    # What the token is bound to: the activity id for a Live Activity
    # update token, the `ActivityAttributes` type name the client starts
    # activities with for a push-to-start token (the start push has to name
    # it as `attributes-type`), empty for a standard token.
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
