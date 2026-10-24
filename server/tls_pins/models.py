"""Pin sets and the signed documents published from them.

Keep in sync with `8996a3bf992f_tls_pin_tables.py`, which also seeds the
two sets the apps ship with.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, LargeBinary, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from server.db import Base


class TlsPinSet(Base):
    __tablename__ = "tls_pin_sets"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    # [{"name": "ntust.edu.tw", "include_subdomains": true}], as published.
    domains: Mapped[list] = mapped_column(JSONB)
    # [{"spki", "label", "first_seen_at", "last_seen_at", "source"}]
    pins: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # "host" or "host:port"; port 443 when left out.
    monitored_hosts: Mapped[list] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    # Whether the watcher may publish the pins it adds.
    auto_publish: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # The watcher's last result per monitored host, for the portal.
    watch_results: Mapped[dict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_by: Mapped[str | None] = mapped_column(Text)


class TlsPinDocument(Base):
    """One signed document. The highest serial is the one served."""

    __tablename__ = "tls_pin_documents"

    serial: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    key_id: Mapped[str] = mapped_column(Text)
    payload: Mapped[bytes] = mapped_column(LargeBinary)
    signature: Mapped[bytes] = mapped_column(LargeBinary)
    # The exact bytes served; never serialised again.
    envelope: Mapped[bytes] = mapped_column(LargeBinary)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    reason: Mapped[str] = mapped_column(Text)
