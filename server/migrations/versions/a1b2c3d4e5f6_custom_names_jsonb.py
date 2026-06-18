"""Rename custom_name (String) to custom_names (JSONB) on user_course_overrides.

Locale-keyed map so cross-device sync can store per-language custom names
independently. Only user-modified names are stored; reverting removes the key.

Revision ID: a1b2c3d4e5f6
Revises: f7e3a6d9c2b1
Create Date: 2026-06-18
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "f7e3a6d9c2b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("user_course_overrides", "custom_name")
    op.add_column(
        "user_course_overrides",
        sa.Column(
            "custom_names",
            postgresql.JSONB(),
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    op.drop_column("user_course_overrides", "custom_names")
    op.add_column(
        "user_course_overrides",
        sa.Column("custom_name", sa.String(256), nullable=True),
    )
