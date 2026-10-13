"""Assignment overrides record when their mark reached the server

Revision ID: bab460b19cc9
Revises: cc54925b7852
Create Date: 2026-10-13 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'bab460b19cc9'
down_revision: Union[str, Sequence[str], None] = 'cc54925b7852'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Keep a note edit from looking like a newly received mark.

    `/sync/full` hides a mark that went out without its Moodle id once a
    later fetch soft-deleted the row it sat on, by comparing when the mark
    reached the server with those soft-deletes. It used `updated_at`,
    which a note edit moves too, so a note-only upload brought a cleared
    mark back. `local_status_received_at` moves only with the mark.

    Existing rows take `updated_at`, the time the rule read until now. The
    column is added without rewriting the table. A second run, after a
    rollback that kept the column, takes `updated_at` again where it moved,
    since the previous release changes marks without this column.
    """
    op.execute(
        "ALTER TABLE user_assignment_overrides"
        " ADD COLUMN IF NOT EXISTS local_status_received_at TIMESTAMP WITH TIME ZONE"
    )
    op.execute(
        "UPDATE user_assignment_overrides SET local_status_received_at = updated_at"
        " WHERE local_status_received_at IS NULL"
        " OR local_status_received_at < updated_at"
    )


def downgrade() -> None:
    """The previous release reads `updated_at` and never this column."""
    op.execute(
        "ALTER TABLE user_assignment_overrides"
        " DROP COLUMN IF EXISTS local_status_received_at"
    )
