"""ORM models for course announcements: the first post of each discussion in
a Moodle course's news forum, its classification, the LLM's extractions and
the review items they become.

Announcement text may name students: it stays in these tables and the
portal, never in a log line, an error column or an app-facing API.
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
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from server.db import Base


class AnnouncementRoute(StrEnum):
    unclassified = "unclassified"
    not_sent = "not_sent"
    sent = "sent"


class ExtractionStatus(StrEnum):
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"
    superseded = "superseded"


class ItemKind(StrEnum):
    course_change = "course_change"
    makeup = "makeup"
    exam = "exam"


class ItemAction(StrEnum):
    add = "add"
    replace = "replace"
    remove = "remove"


class ItemState(StrEnum):
    pending = "pending"
    applied = "applied"
    approved = "approved"
    rejected = "rejected"
    past = "past"
    superseded = "superseded"
    info = "info"
    skipped = "skipped"
    edited = "edited"
    removed = "removed"


SUPERSEDABLE_ITEM_STATES = (ItemState.pending.value, ItemState.info.value)


# The predicate of `ux_course_announcement_extractions_live`, as SQL text for
# an ON CONFLICT to repeat (server/AGENTS.md on partial unique indexes).
EXTRACTION_LIVE_WHERE = "status IN ('queued', 'running')"


class CourseAnnouncementCourse(Base):
    """The poll state of one current-term Moodle course."""

    __tablename__ = "course_announcement_courses"

    moodle_course_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    term_code: Mapped[str] = mapped_column(String(8))
    course_codes: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default="{}"
    )
    # The course's `news` forum; None until a lookup found one.
    forum_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    latest_post_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # None: never polled, so due before every course that was.
    next_due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Whose token the next poll tries first.
    holder_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Polls in a row a holder failed since the last one that read the forum.
    holder_failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CourseAnnouncement(Base):
    """One version of an announcement: the one with the latest `modified_at`
    seen, whether polling or the import stored it first."""

    __tablename__ = "course_announcements"

    # "{discussion id}-{first post id}".
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    term_code: Mapped[str] = mapped_column(String(8))
    moodle_course_id: Mapped[int] = mapped_column(BigInteger)
    # Every course number of the Moodle course, merged sections included.
    course_codes: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default="{}"
    )
    # Moodle's subject and message HTML, what the classifier converts; kept
    # only until it has.
    raw_subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    subject: Mapped[str] = mapped_column(Text)
    # The classifier's conversion: the title, a blank line and the Markdown body.
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    modified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Moodle's group, -1 for all participants.
    group_id: Mapped[int] = mapped_column(BigInteger)
    # When the post becomes visible to students, None when it is from the start.
    visible_from: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    has_attachment: Mapped[bool] = mapped_column(Boolean)
    classifier_release: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Every label's score, as the service returned them.
    scores: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # The labels at or above the service's threshold.
    labels: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    route: Mapped[str] = mapped_column(
        String(16),
        default=AnnouncementRoute.unclassified.value,
        server_default=AnnouncementRoute.unclassified.value,
    )
    # When the service last answered this version with no result.
    classify_failed_at: Mapped[datetime | None] = mapped_column(
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
            "route IN ('unclassified', 'not_sent', 'sent')", name="chk_course_announcement_route"
        ),
        CheckConstraint(
            "CASE WHEN route = 'unclassified'"
            " THEN raw_subject IS NOT NULL AND raw_message IS NOT NULL"
            " ELSE raw_subject IS NULL AND raw_message IS NULL"
            " AND text IS NOT NULL AND scores IS NOT NULL AND labels IS NOT NULL END",
            name="chk_course_announcement_classified",
        ),
        Index("ix_course_announcements_term_posted", "term_code", "posted_at"),
        # In `ingest.classify_pending`'s order.
        Index(
            "ix_course_announcements_unclassified",
            sa.text("classify_failed_at NULLS FIRST"),
            "posted_at",
            "id",
            postgresql_where=sa.text("route = 'unclassified'"),
        ),
    )


class CourseAnnouncementExtraction(Base):
    """One LLM extraction of one announcement version."""

    __tablename__ = "course_announcement_extractions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    announcement_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("course_announcements.id", ondelete="CASCADE")
    )
    # The announcement's `modified_at` it was queued for.
    version: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_output: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    certain: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16),
        default=ExtractionStatus.queued.value,
        server_default=ExtractionStatus.queued.value,
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # A fixed code, never model output or announcement text.
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'done', 'failed', 'superseded')",
            name="chk_course_announcement_extraction_status",
        ),
        CheckConstraint("attempts >= 0", name="chk_course_announcement_extraction_attempts"),
        Index(
            "ux_course_announcement_extractions_live",
            "announcement_id",
            "version",
            unique=True,
            postgresql_where=sa.text(EXTRACTION_LIVE_WHERE),
        ),
        Index("ix_course_announcement_extractions_announcement", "announcement_id"),
    )


class CourseAnnouncementItem(Base):
    """One extracted fact for one course code, and what became of it."""

    __tablename__ = "course_announcement_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    extraction_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("course_announcement_extractions.id", ondelete="CASCADE")
    )
    course_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(16))
    action: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # The fact in `course_changes` field names.
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    # The applied change it replaces or removes, or the change it wrote once
    # applied or approved; editing or deleting that change marks the item.
    course_change_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("course_changes.id", ondelete="SET NULL"), nullable=True
    )
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    checks: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    passes_checks: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sa.text("false")
    )
    state: Mapped[str] = mapped_column(
        String(16), default=ItemState.pending.value, server_default=ItemState.pending.value
    )
    # The operator's Cloudflare Access email, or `course-announcements`.
    decided_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "kind IN ('course_change', 'makeup', 'exam')",
            name="chk_course_announcement_item_kind",
        ),
        CheckConstraint(
            "action IS NULL OR action IN ('add', 'replace', 'remove')",
            name="chk_course_announcement_item_action",
        ),
        CheckConstraint(
            "state IN ('pending', 'applied', 'approved', 'rejected', 'past', 'superseded',"
            " 'info', 'skipped', 'edited', 'removed')", name="chk_course_announcement_item_state"
        ),
        Index("ix_course_announcement_items_extraction", "extraction_id"),
        Index(
            "ix_course_announcement_items_pending",
            "extraction_id",
            postgresql_where=sa.text("state = 'pending'"),
        ),
    )
