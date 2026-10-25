"""The school's course catalog, one snapshot per semester and language, and
the en course text the snapshots in en are built with.

Keep in sync with `5d2a9c7e4f1b_course_catalog_snapshots.py`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, LargeBinary, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from server.db import Base


class CourseCatalogSnapshot(Base):
    """The last complete fetch of a semester in one language. A refresh
    replaces it only when every request it made succeeded and the content
    changed."""

    __tablename__ = "course_catalog_snapshots"

    semester: Mapped[str] = mapped_column(Text, primary_key=True)
    # "zh" or "en", the school's `Language`.
    language: Mapped[str] = mapped_column(Text, primary_key=True)
    # The gzip-compressed JSON served; never serialised again.
    body: Mapped[bytes] = mapped_column(LargeBinary)
    # Hashes the content without `fetched_at`.
    etag: Mapped[str] = mapped_column(Text)
    # When this content was first fetched.
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CourseCatalogEnglish(Base):
    """A semester's en course text from its last en sweep, which runs once a
    day. Every refresh builds the en snapshot from its zh rows and this."""

    __tablename__ = "course_catalog_en_texts"

    semester: Mapped[str] = mapped_column(Text, primary_key=True)
    # "CourseNo#Node" -> [CourseName, CourseTeacher, Contents]
    texts: Mapped[dict] = mapped_column(JSONB)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
