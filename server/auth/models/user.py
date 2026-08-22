"""The user record and the external (NTUST, Moodle) accounts and stored
credentials hanging off it."""

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
# User and UserDevice reference each other, so a runtime import here
# would be a cycle. Both references are annotations, and with
# `from __future__ import annotations` SQLAlchemy resolves the target
# through its class registry rather than this module's globals — so
# the import is only needed by type checkers.
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .device import UserDevice
from .enums import CredentialStatus, UserStatus


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
