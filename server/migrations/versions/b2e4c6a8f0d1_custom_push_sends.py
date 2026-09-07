"""Add custom_push_sends — operator custom-push history.

The portal's Custom Push page now delivers via the v3 push pipeline
(one push_jobs row per target). This table records one summary row per
send so the "recent sends" view can list history without scanning
push_jobs. Written by the portal, read by the portal; the backend
ignores it.

Revision ID: b2e4c6a8f0d1
Revises: a8c2f1e0d4b6
Create Date: 2026-06-18
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b2e4c6a8f0d1"
down_revision = "a8c2f1e0d4b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "custom_push_sends",
        sa.Column("request_id", sa.String(length=32), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        # 'record' (also stored as a bulletin) or 'popup' (ephemeral).
        sa.Column("kind", sa.String(length=16), nullable=False),
        # Comma-joined target classes, e.g. "iphone,android".
        sa.Column("target_classes", sa.String(length=128), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_custom_push_sends_created_at", "custom_push_sends", ["created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_custom_push_sends_created_at", "custom_push_sends")
    op.drop_table("custom_push_sends")
