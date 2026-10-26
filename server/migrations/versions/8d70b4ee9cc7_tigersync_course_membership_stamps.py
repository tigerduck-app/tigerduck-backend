"""A course add has its stamp, and each term reset its own row

Revision ID: 8d70b4ee9cc7
Revises: 88dbb93dba99
Create Date: 2026-10-26 18:00:00.000000

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '8d70b4ee9cc7'
down_revision: str | Sequence[str] | None = '88dbb93dba99'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Stamp the membership choices already stored (`server/sync/membership.py`).

    A course added by hand takes its row's `created_at` as its add stamp, and
    each term reset takes the latest time of its reset tombstones. The column
    is added without rewriting the table, and only rows added by hand are
    updated. A second run, after a rollback that kept both, stamps the adds
    made meanwhile and raises a reset to a later reset made meanwhile.
    """
    op.execute("ALTER TABLE user_courses ADD COLUMN IF NOT EXISTS added_at timestamptz")
    op.execute(
        "UPDATE user_courses SET added_at = created_at"
        " WHERE source = 'user_added' AND added_at IS NULL"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS user_semester_resets (
            user_id  uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
            semester varchar(16) NOT NULL,
            reset_at timestamptz NOT NULL,
            PRIMARY KEY (user_id, semester)
        )
        """
    )
    op.execute(
        """
        INSERT INTO user_semester_resets (user_id, semester, reset_at)
        SELECT user_id, semester, max(deleted_at)
        FROM user_course_tombstones
        WHERE deleted_by_reset
        GROUP BY user_id, semester
        ON CONFLICT (user_id, semester) DO UPDATE
        SET reset_at = GREATEST(user_semester_resets.reset_at, EXCLUDED.reset_at)
        """
    )


def downgrade() -> None:
    """The previous release hides every course a tombstone names, so the
    removal tombstones kept beside a roster row go with the stamps."""
    op.execute(
        """
        DELETE FROM user_course_tombstones t
        USING user_courses c
        WHERE t.user_id = c.user_id AND t.course_key = c.course_key
          AND t.user_added AND NOT t.deleted_by_reset
        """
    )
    op.execute("DROP TABLE IF EXISTS user_semester_resets")
    op.execute("ALTER TABLE user_courses DROP COLUMN IF EXISTS added_at")
