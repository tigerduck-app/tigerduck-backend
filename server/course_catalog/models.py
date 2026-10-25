"""The school's course catalog, one snapshot per semester and language.

Keep in sync with `5d2a9c7e4f1b_course_catalog_snapshots.py`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, LargeBinary, Text
from sqlalchemy.orm import Mapped, mapped_column

from server.db import Base


class CourseCatalogSnapshot(Base):
    """The last complete fetch of a semester in one language. A refresh
    replaces it only when every request it made succeeded."""

    __tablename__ = "course_catalog_snapshots"

    semester: Mapped[str] = mapped_column(Text, primary_key=True)
    # "zh" or "en", the school's `Language`.
    language: Mapped[str] = mapped_column(Text, primary_key=True)
    # The gzip-compressed JSON served; never serialised again.
    body: Mapped[bytes] = mapped_column(LargeBinary)
    etag: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
