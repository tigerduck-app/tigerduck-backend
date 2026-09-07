"""device lists schema

Revision ID: d4f8a1c3e2b7
Revises: c1d8e3f4a9b2
Create Date: 2026-05-26 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4f8a1c3e2b7"
down_revision: Union[str, Sequence[str], None] = "c1d8e3f4a9b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "device_lists",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    op.create_table(
        "device_list_members",
        sa.Column("list_id", sa.BigInteger(), nullable=False),
        sa.Column("device_id", sa.String(length=128), nullable=False),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["list_id"],
            ["device_lists.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["device_registrations.device_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("list_id", "device_id"),
    )
    op.create_index(
        "ix_device_list_members_device_id",
        "device_list_members",
        ["device_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_device_list_members_device_id", table_name="device_list_members"
    )
    op.drop_table("device_list_members")
    op.drop_table("device_lists")
