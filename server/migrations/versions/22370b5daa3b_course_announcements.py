"""course announcements: users' Moodle courses, poll state, announcements,
extractions, review items and course_changes.source_announcement_id

Revision ID: 22370b5daa3b
Revises: bbb33844e87e
Create Date: 2026-11-15 18:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '22370b5daa3b'
down_revision: str | Sequence[str] | None = 'bbb33844e87e'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    ]


def upgrade() -> None:
    """The tables server/syncjobs/models.py (`UserMoodleCourse`) and
    server/course_announcements/models.py declare, and the source column of
    `CourseChange`. A second run, after a rollback that kept them, changes
    nothing."""
    op.create_table(
        "user_moodle_courses",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("moodle_course_id", sa.BigInteger(), primary_key=True),
        sa.Column("idnumber", sa.Text(), nullable=False),
        sa.Column("full_name", sa.Text(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        if_not_exists=True,
    )
    op.create_index(
        "ix_user_moodle_courses_course",
        "user_moodle_courses",
        ["moodle_course_id"],
        if_not_exists=True,
    )

    op.create_table(
        "course_announcement_courses",
        sa.Column("moodle_course_id", sa.BigInteger(), primary_key=True),
        sa.Column("term_code", sa.String(length=8), nullable=False),
        sa.Column(
            "course_codes", postgresql.ARRAY(sa.Text()), nullable=False, server_default="{}"
        ),
        sa.Column("forum_id", sa.BigInteger(), nullable=True),
        sa.Column("latest_post_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "holder_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        if_not_exists=True,
    )
    op.create_index(
        "ix_course_announcement_courses_due",
        "course_announcement_courses",
        ["term_code", "next_due_at"],
        if_not_exists=True,
    )

    op.create_table(
        "course_announcements",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("term_code", sa.String(length=8), nullable=False),
        sa.Column("moodle_course_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "course_codes", postgresql.ARRAY(sa.Text()), nullable=False, server_default="{}"
        ),
        sa.Column("raw_subject", sa.Text(), nullable=True),
        sa.Column("raw_message", sa.Text(), nullable=True),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("modified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("visible_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("has_attachment", sa.Boolean(), nullable=False),
        sa.Column("classifier_release", sa.Text(), nullable=True),
        sa.Column("scores", postgresql.JSONB(), nullable=True),
        sa.Column("labels", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column(
            "route", sa.String(length=16), nullable=False, server_default="unclassified"
        ),
        sa.Column("classify_failed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "route IN ('unclassified', 'not_sent', 'sent')",
            name="chk_course_announcement_route",
        ),
        sa.CheckConstraint(
            "CASE WHEN route = 'unclassified'"
            " THEN raw_subject IS NOT NULL AND raw_message IS NOT NULL"
            " ELSE raw_subject IS NULL AND raw_message IS NULL"
            " AND text IS NOT NULL AND scores IS NOT NULL AND labels IS NOT NULL END",
            name="chk_course_announcement_classified",
        ),
        if_not_exists=True,
    )
    op.create_index(
        "ix_course_announcements_term_posted",
        "course_announcements",
        ["term_code", "posted_at"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_course_announcements_unclassified",
        "course_announcements",
        ["posted_at"],
        postgresql_where=sa.text("route = 'unclassified'"),
        if_not_exists=True,
    )

    op.create_table(
        "course_announcement_extractions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "announcement_id",
            sa.String(length=64),
            sa.ForeignKey("course_announcements.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.DateTime(timezone=True), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("raw_output", postgresql.JSONB(), nullable=True),
        sa.Column("certain", sa.Boolean(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'done', 'failed', 'superseded')",
            name="chk_course_announcement_extraction_status",
        ),
        sa.CheckConstraint(
            "attempts >= 0", name="chk_course_announcement_extraction_attempts"
        ),
        if_not_exists=True,
    )
    op.create_index(
        "ux_course_announcement_extractions_live",
        "course_announcement_extractions",
        ["announcement_id", "version"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
        if_not_exists=True,
    )
    op.create_index(
        "ix_course_announcement_extractions_announcement",
        "course_announcement_extractions",
        ["announcement_id"],
        if_not_exists=True,
    )

    op.create_table(
        "course_announcement_items",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "extraction_id",
            sa.BigInteger(),
            sa.ForeignKey("course_announcement_extractions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("course_no", sa.String(length=64), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("action", sa.String(length=8), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "course_change_id",
            sa.Integer(),
            sa.ForeignKey("course_changes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("checks", postgresql.JSONB(), nullable=True),
        sa.Column(
            "passes_checks", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "kind IN ('course_change', 'makeup', 'exam')",
            name="chk_course_announcement_item_kind",
        ),
        sa.CheckConstraint(
            "action IS NULL OR action IN ('add', 'replace', 'remove')",
            name="chk_course_announcement_item_action",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'applied', 'approved', 'rejected', 'past', 'superseded',"
            " 'info', 'skipped', 'edited', 'removed')",
            name="chk_course_announcement_item_state",
        ),
        if_not_exists=True,
    )
    op.create_index(
        "ix_course_announcement_items_extraction",
        "course_announcement_items",
        ["extraction_id"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_course_announcement_items_pending",
        "course_announcement_items",
        ["extraction_id"],
        postgresql_where=sa.text("state = 'pending'"),
        if_not_exists=True,
    )

    op.execute(
        "ALTER TABLE course_changes ADD COLUMN IF NOT EXISTS source_announcement_id"
        " VARCHAR(64) REFERENCES course_announcements (id) ON DELETE SET NULL"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE course_changes DROP COLUMN IF EXISTS source_announcement_id")
    op.drop_table("course_announcement_items", if_exists=True)
    op.drop_table("course_announcement_extractions", if_exists=True)
    op.drop_table("course_announcements", if_exists=True)
    op.drop_table("course_announcement_courses", if_exists=True)
    op.drop_table("user_moodle_courses", if_exists=True)
