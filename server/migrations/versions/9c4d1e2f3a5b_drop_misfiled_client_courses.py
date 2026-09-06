"""drop_misfiled_client_courses

Revision ID: 9c4d1e2f3a5b
Revises: f7e3a6d9c2b1
Create Date: 2026-09-07 06:00:00.000000

"""
from collections.abc import Sequence

from alembic import op

from server.sync.misfiled_courses import MISFILED_CLIENT_COURSES_DELETE

# revision identifiers, used by Alembic.
revision: str = '9c4d1e2f3a5b'
down_revision: str | Sequence[str] | None = 'f7e3a6d9c2b1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Data only: delete client course rows whose Moodle id names a
    different term than the one they are filed under (see
    server/sync/misfiled_courses.py). Overrides and skipped dates cascade."""
    op.execute(MISFILED_CLIENT_COURSES_DELETE)


def downgrade() -> None:
    """Nothing to restore — the next client sync re-uploads the correct
    roster under the right term."""
