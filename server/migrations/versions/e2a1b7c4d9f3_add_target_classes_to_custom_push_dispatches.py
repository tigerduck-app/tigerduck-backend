"""add target_classes to custom_push_dispatches

Revision ID: e2a1b7c4d9f3
Revises: d4f8a1c3e2b7
Create Date: 2026-05-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e2a1b7c4d9f3"
down_revision: Union[str, Sequence[str], None] = "d4f8a1c3e2b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "custom_push_dispatches",
        sa.Column(
            "target_classes",
            sa.String(length=64),
            nullable=False,
            server_default="",
        ),
    )


def downgrade() -> None:
    op.drop_column("custom_push_dispatches", "target_classes")
