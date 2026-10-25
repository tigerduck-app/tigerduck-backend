"""course_catalog_snapshots: the school's course catalog per semester and language

Revision ID: 5d2a9c7e4f1b
Revises: 8996a3bf992f
Create Date: 2026-10-26 12:00:00.000000

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '5d2a9c7e4f1b'
down_revision: str | Sequence[str] | None = '8996a3bf992f'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """The daily refresh (`server/course_catalog/refresh.py`) fills it and
    `GET /v3/course-catalog/{semester}` serves it. A second run changes
    nothing.
    """
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS course_catalog_snapshots (
            semester   text NOT NULL,
            language   text NOT NULL,
            body       bytea NOT NULL,
            etag       text NOT NULL,
            fetched_at timestamptz NOT NULL,
            PRIMARY KEY (semester, language)
        )
        """
    )


def downgrade() -> None:
    """The previous release neither reads nor writes it; the next refresh
    after an upgrade fills it again."""
    op.execute("DROP TABLE IF EXISTS course_catalog_snapshots")
