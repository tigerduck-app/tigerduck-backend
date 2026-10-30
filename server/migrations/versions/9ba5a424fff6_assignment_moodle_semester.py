"""user_assignments.moodle_semester: the term of an assignment's Moodle course

The reminder scan files only for assignments the iPhone app lists in the
current term (tigerduck-backend#81), which it tells from this column and the
row's link to a course of the student's class table. Both come from the
server's sync, which now also reads the student's Moodle courses. No stored
data holds a course's Moodle idnumber, so existing rows are not backfilled:
the scan reminds of each as before until its user's next sync classifies it.

Revision ID: 9ba5a424fff6
Revises: 199cfb4bbb49
Create Date: 2026-10-30 12:00:00.000000

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '9ba5a424fff6'
down_revision: str | Sequence[str] | None = '199cfb4bbb49'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Added without rewriting the table; a second run changes nothing."""
    op.execute(
        "ALTER TABLE user_assignments"
        " ADD COLUMN IF NOT EXISTS moodle_semester VARCHAR(16)"
    )


def downgrade() -> None:
    """The previous release neither reads nor writes it, and reminds of
    every assignment again."""
    op.execute("ALTER TABLE user_assignments DROP COLUMN IF EXISTS moodle_semester")
