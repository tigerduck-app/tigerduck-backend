"""user_assignments keep the student's extension

Revision ID: c5e8a2f71d40
Revises: d07bc39dde6c
Create Date: 2026-10-23 18:00:00.000000

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c5e8a2f71d40'
down_revision: str | Sequence[str] | None = 'd07bc39dde6c'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """`due_at` and `cutoff_at` become the dates that apply to the student,
    Moodle's moved by the student's extension. The assignment list carries
    no extension, and the submission probe asks only about some
    assignments, so the list sync needs the last extension Moodle gave to
    keep a date it cannot see from going back to Moodle's.

    Existing rows start without one; the next probe of each fills it in.
    Added without rewriting the table; a second run changes nothing.
    """
    op.execute(
        "ALTER TABLE user_assignments"
        " ADD COLUMN IF NOT EXISTS extension_due_at TIMESTAMP WITH TIME ZONE"
    )


def downgrade() -> None:
    """The previous release neither reads nor writes it, and its next
    fetch puts Moodle's dates back."""
    op.execute("ALTER TABLE user_assignments DROP COLUMN IF EXISTS extension_due_at")
