"""Rename custom_name (String) to custom_names (JSONB) on user_course_overrides.

Locale-keyed map so cross-device sync can store per-language custom names
independently. Only user-modified names are stored; reverting removes the key.
Existing singular names are preserved under both 'zh' and 'en' — the only
locale keys the iOS/Android apps and the portal ever read — because the
singular column was locale-less (one name shown in every app language).

Revision ID: a1b2c3d4e5f6
Revises: b2e4c6a8f0d1
Create Date: 2026-06-18
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "b2e4c6a8f0d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "user_course_overrides",
        sa.Column(
            "custom_names",
            postgresql.JSONB(),
            nullable=False,
            server_default="{}",
        ),
    )
    # Preserve existing singular names before dropping the column. Empty
    # string meant "cleared" in the singular world, so only non-empty
    # names carry over. The singular name was shown regardless of app
    # language, so seed both keys the clients read ('zh' and 'en').
    op.execute(
        """
        UPDATE user_course_overrides
        SET custom_names = jsonb_build_object(
            'zh', custom_name,
            'en', custom_name
        )
        WHERE custom_name IS NOT NULL AND custom_name <> ''
        """
    )
    op.drop_column("user_course_overrides", "custom_name")


def downgrade() -> None:
    op.add_column(
        "user_course_overrides",
        sa.Column("custom_name", sa.String(256), nullable=True),
    )
    # Collapse the locale map back to one name: prefer 'zh' (the primary
    # app locale), else fall back to any stored locale.
    op.execute(
        """
        UPDATE user_course_overrides
        SET custom_name = left(
            COALESCE(
                custom_names->>'zh',
                (
                    SELECT t.value
                    FROM jsonb_each_text(custom_names) AS t
                    ORDER BY t.key
                    LIMIT 1
                )
            ),
            256
        )
        WHERE custom_names <> '{}'::jsonb
        """
    )
    op.drop_column("user_course_overrides", "custom_names")
