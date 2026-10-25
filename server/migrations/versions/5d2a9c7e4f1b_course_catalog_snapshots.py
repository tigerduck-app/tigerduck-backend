"""course_catalog_snapshots and course_catalog_en_texts: the school's course catalog

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
    """The refresh (`server/course_catalog/refresh.py`) fills both;
    `GET /v3/course-catalog/{semester}` serves the snapshots, and the en text
    of the day's en sweep builds the en ones. A second run changes nothing.
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
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS course_catalog_en_texts (
            semester   text PRIMARY KEY,
            texts      jsonb NOT NULL,
            fetched_at timestamptz NOT NULL
        )
        """
    )


def downgrade() -> None:
    """The previous release neither reads nor writes them; the next refresh
    after an upgrade fills them again."""
    op.execute("DROP TABLE IF EXISTS course_catalog_en_texts")
    op.execute("DROP TABLE IF EXISTS course_catalog_snapshots")
