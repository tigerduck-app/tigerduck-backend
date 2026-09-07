"""add_device_sync_preferences

Revision ID: c7d8e9f0a1b2
Revises: a9f104262be3
Create Date: 2026-06-21 09:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7d8e9f0a1b2'
down_revision: Union[str, Sequence[str], None] = 'a9f104262be3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "user_devices",
        sa.Column("sync_courses", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )
    op.add_column(
        "user_devices",
        sa.Column("sync_course_colors", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )
    op.add_column(
        "user_devices",
        sa.Column("sync_course_names", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )
    op.add_column(
        "user_devices",
        sa.Column("sync_assignments", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("user_devices", "sync_assignments")
    op.drop_column("user_devices", "sync_course_names")
    op.drop_column("user_devices", "sync_course_colors")
    op.drop_column("user_devices", "sync_courses")
