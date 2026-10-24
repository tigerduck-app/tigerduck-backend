"""The `links` table. Keep it in step with `35df7613737a_links.py`."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from server.db import Base

ALERT_OK = "ok"
ALERT_FAILING = "failing"


class Link(Base):
    __tablename__ = "links"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    target_url: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    # Rules in the shapes `server.links.rules` accepts.
    expectations: Mapped[list] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    check_interval_minutes: Mapped[int] = mapped_column(
        Integer, server_default=text("60")
    )
    # When the recorded check started; a check that started earlier and
    # ended later records nothing over it.
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_check_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_check_detail: Mapped[str | None] = mapped_column(Text)
    # ALERT_OK or ALERT_FAILING: what the alert last said, so a restart
    # neither repeats a failure alert nor misses a recovery.
    alert_state: Mapped[str] = mapped_column(Text, server_default=text("'ok'"))
    # The detail of the check that started the failure. The alert is labelled
    # with it until the link recovers, so its label set survives a restart.
    alert_detail: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # The operator's Cloudflare Access email, as admin_audit_log records it.
    updated_by: Mapped[str] = mapped_column(Text)
