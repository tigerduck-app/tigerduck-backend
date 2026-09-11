"""Retire the v2 push tables and the leftover user_courses.deleted_at.

Three schema objects outlived the code that used them. Each was left behind by
a commit that removed its last reader and writer but shipped no migration, so
`alembic revision --autogenerate` has been proposing to remove all three on
every run since -- mixed in with whatever change the author actually wanted.

* `scheduled_pushes`, `live_activity_update_tokens` -- `bfa32fc`
  ("delete the v2 push machinery that nothing can reach") removed the
  `ScheduledPush` and `LiveActivityUpdateToken` models, both dispatcher loops,
  and the only routes that wrote them. v1/v2 answer 410 Gone and only the v3
  routers are mounted, so nothing has written either table since; Live Activity
  update tokens now live in `device_push_tokens` with
  `token_kind='live_activity_update'`.
* `user_courses.deleted_at` -- `b746295` ("hard-delete courses and assignment
  overrides instead of soft-delete/hide") took the column out of the model and
  the `WHERE deleted_at IS NULL` predicate off `idx_user_courses_semester`. Its
  sibling migration `4e6c604ad58c` handled the matching
  `user_course_overrides.is_hidden*` columns; this column was missed. Soft
  deletion moved to the `user_course_tombstones` table.

Irreversible in practice: `downgrade()` restores the structures but not their
contents. Rows written before each feature was retired go away here. To see
what that costs before deploying:

    SELECT count(*) FROM scheduled_pushes;
    SELECT count(*) FROM live_activity_update_tokens;
    SELECT count(*) FROM user_courses WHERE deleted_at IS NOT NULL;

Revision ID: c1e7a94b8f20
Revises: ab1bc4fcf33c
Create Date: 2026-09-11
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c1e7a94b8f20"
down_revision: Union[str, Sequence[str], None] = "ab1bc4fcf33c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index(
        "ix_live_activity_tokens_due", table_name="live_activity_update_tokens"
    )
    op.drop_index(
        "ix_live_activity_update_tokens_device_id",
        table_name="live_activity_update_tokens",
    )
    op.drop_index(
        "ix_live_activity_update_tokens_countdown_target",
        table_name="live_activity_update_tokens",
    )
    op.drop_table("live_activity_update_tokens")

    op.drop_index("ix_pushes_due", table_name="scheduled_pushes")
    op.drop_index("ix_scheduled_pushes_device_id", table_name="scheduled_pushes")
    op.drop_index("ix_scheduled_pushes_fire_at", table_name="scheduled_pushes")
    op.drop_table("scheduled_pushes")

    # The index is partial on the column going away, so it has to be rebuilt
    # without the predicate -- which is exactly how the model has declared it
    # since b746295.
    op.drop_index(
        "idx_user_courses_semester",
        table_name="user_courses",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.drop_column("user_courses", "deleted_at")
    op.create_index("idx_user_courses_semester", "user_courses", ["user_id", "semester"])

    # `SystemSetting.updated_at` has been non-optional in the model since the
    # table was introduced, and `server_default=now()` means no row can reach
    # NULL through normal writes -- but the migration that created the table
    # left the column nullable. Backfill defensively, then make the database
    # agree with the model.
    op.execute(
        sa.text("UPDATE system_settings SET updated_at = now() WHERE updated_at IS NULL")
    )
    op.alter_column(
        "system_settings",
        "updated_at",
        existing_type=sa.DateTime(timezone=True),
        existing_server_default=sa.text("now()"),
        nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "system_settings",
        "updated_at",
        existing_type=sa.DateTime(timezone=True),
        existing_server_default=sa.text("now()"),
        nullable=True,
    )

    op.drop_index("idx_user_courses_semester", table_name="user_courses")
    op.add_column(
        "user_courses",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "idx_user_courses_semester",
        "user_courses",
        ["user_id", "semester"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "scheduled_pushes",
        sa.Column("push_id", sa.String(length=256), nullable=False),
        sa.Column("device_id", sa.String(length=128), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("scenario", sa.String(length=32), nullable=False),
        sa.Column("fire_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.BigInteger(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["device_id"], ["device_registrations.device_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("push_id"),
    )
    op.create_index("ix_pushes_due", "scheduled_pushes", ["status", "fire_at"])
    op.create_index("ix_scheduled_pushes_device_id", "scheduled_pushes", ["device_id"])
    op.create_index("ix_scheduled_pushes_fire_at", "scheduled_pushes", ["fire_at"])

    op.create_table(
        "live_activity_update_tokens",
        sa.Column("activity_id", sa.String(length=256), nullable=False),
        sa.Column("device_id", sa.String(length=128), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("scenario", sa.String(length=32), nullable=False),
        sa.Column("update_token_hex", sa.String(length=512), nullable=False),
        sa.Column("countdown_target", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "snapshot_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.BigInteger(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["device_id"], ["device_registrations.device_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("activity_id"),
    )
    op.create_index(
        "ix_live_activity_update_tokens_countdown_target",
        "live_activity_update_tokens",
        ["countdown_target"],
    )
    op.create_index(
        "ix_live_activity_update_tokens_device_id",
        "live_activity_update_tokens",
        ["device_id"],
    )
    op.create_index(
        "ix_live_activity_tokens_due",
        "live_activity_update_tokens",
        ["status", "countdown_target"],
    )
