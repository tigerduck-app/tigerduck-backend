"""Per-namespace settings documents synced between a user's devices."""

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


class UserSettingsDocument(Base):
    """One JSONB settings document per (user, namespace). Revision is
    server-incremented; clients submit base_revision for optimistic
    concurrency (sync-and-push spec §2/§6)."""

    __tablename__ = "user_settings_documents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    namespace: Mapped[str] = mapped_column(String(64))
    schema_version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1"
    )
    document: Mapped[dict] = mapped_column(JSONB)
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
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "namespace IN ("
            "'home_layout', 'appearance', 'assignment_display', "
            "'notification', 'browser', 'language', "
            "'schedule_display', 'watch', 'wearos')",
            name="chk_settings_namespace",
        ),
        CheckConstraint("revision >= 1", name="chk_settings_revision"),
        CheckConstraint(
            "schema_version >= 1", name="chk_settings_schema_version"
        ),
        Index(
            "ux_user_settings_namespace_active",
            "user_id",
            "namespace",
            unique=True,
            postgresql_where=sa.text("deleted_at IS NULL"),
        ),
        Index(
            "idx_user_settings_user_updated",
            "user_id",
            "updated_at",
            postgresql_where=sa.text("deleted_at IS NULL"),
        ),
    )
