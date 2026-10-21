"""Course tombstones record whether the deleted course was added by hand

Revision ID: a463951f49a7
Revises: d899fe7d1f79
Create Date: 2026-10-21 22:00:00.000000

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a463951f49a7'
down_revision: str | Sequence[str] | None = 'd899fe7d1f79'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Let the roster bring back a course the user added by hand and removed.

    Removing such a course leaves the tombstone that hiding an enrolled one
    does, so once the student really took the course, no upload could bring
    it back. A tombstone marked `user_added` does not bind the device that
    wrote it, and that device's snapshot leaves it out.

    Existing tombstones take false and keep binding: the rows they came from
    are gone, and the course upload stored a course added by hand like any
    other, so nothing tells them apart. The column is added without
    rewriting the table, and a second run changes nothing.
    """
    op.execute(
        "ALTER TABLE user_course_tombstones"
        " ADD COLUMN IF NOT EXISTS user_added BOOLEAN NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    """The previous release binds every tombstone as a hidden course's."""
    op.execute("ALTER TABLE user_course_tombstones DROP COLUMN IF EXISTS user_added")
