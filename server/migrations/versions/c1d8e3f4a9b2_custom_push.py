"""custom push schema

Revision ID: c1d8e3f4a9b2
Revises: b6f4c9a2d1e0
Create Date: 2026-05-25 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c1d8e3f4a9b2"
down_revision: Union[str, Sequence[str], None] = "b6f4c9a2d1e0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "device_registrations",
        sa.Column(
            "device_class",
            sa.String(length=16),
            nullable=False,
            server_default="",
        ),
    )
    op.add_column(
        "device_registrations",
        sa.Column(
            "server_push_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.create_index(
        "ix_devices_class_enabled",
        "device_registrations",
        ["device_class"],
        unique=False,
        postgresql_where=sa.text("server_push_enabled = true"),
    )

    op.add_column(
        "bulletins",
        sa.Column(
            "dispatch_filter_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )

    op.create_table(
        "custom_push_dispatches",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.String(length=32), nullable=False),
        sa.Column("device_id", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("force_ring", sa.Boolean(), nullable=False),
        sa.Column("notification_id", sa.String(length=64), nullable=False),
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
            ["device_id"],
            ["device_registrations.device_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_custom_push_dispatches_request_id",
        "custom_push_dispatches",
        ["request_id"],
        unique=False,
    )
    op.create_index(
        "ix_custom_push_dispatches_device_id",
        "custom_push_dispatches",
        ["device_id"],
        unique=False,
    )
    op.create_index(
        "ix_custom_push_pending",
        "custom_push_dispatches",
        ["status"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_custom_push_pending", table_name="custom_push_dispatches"
    )
    op.drop_index(
        "ix_custom_push_dispatches_device_id", table_name="custom_push_dispatches"
    )
    op.drop_index(
        "ix_custom_push_dispatches_request_id", table_name="custom_push_dispatches"
    )
    op.drop_table("custom_push_dispatches")

    op.drop_column("bulletins", "dispatch_filter_json")

    op.drop_index(
        "ix_devices_class_enabled", table_name="device_registrations"
    )
    op.drop_column("device_registrations", "server_push_enabled")
    op.drop_column("device_registrations", "device_class")
