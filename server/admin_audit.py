"""Who did what in the portal, to which account or device.

The portal writes these rows with raw SQL (`portal/app/audit.py`); the backend
owns the table and prunes rows older than `admin_audit_retention_days`. Keep
the model in sync with `d07bc39dde6c_admin_audit_log.py`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import BigInteger, DateTime, Index, Text, delete, func, text
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from server.config import Settings
from server.db import Base

logger = structlog.get_logger(__name__)


class AdminAuditLog(Base):
    __tablename__ = "admin_audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # The operator's Cloudflare Access email, or "dev".
    actor: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    # No foreign keys: the record outlives the account or device it names.
    target_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    target_device_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    detail: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    ip: Mapped[str | None] = mapped_column(INET)

    __table_args__ = (
        Index("idx_admin_audit_ts", text("ts DESC")),
        Index(
            "idx_admin_audit_target_user",
            "target_user_id",
            text("ts DESC"),
            postgresql_where=text("target_user_id IS NOT NULL"),
        ),
    )


async def prune_admin_audit_log(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(
        days=settings.admin_audit_retention_days
    )
    async with session_factory() as session:
        result = await session.execute(
            delete(AdminAuditLog).where(AdminAuditLog.ts < cutoff)
        )
        await session.commit()
    deleted = result.rowcount or 0
    logger.info(
        "admin_audit.retention.done",
        deleted=deleted,
        cutoff=cutoff.isoformat(),
        retention_days=settings.admin_audit_retention_days,
    )
    return deleted
