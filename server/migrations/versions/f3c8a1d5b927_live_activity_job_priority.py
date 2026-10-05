"""Live Activity jobs already queued go first

Live Activity starts and ends are now filed at priority 10, ahead of the
column default of 100, so a class's activity is not held behind a backlog
of other pushes. Jobs filed before that kept 100: `/schedule/sync` leaves
an existing start as it is, and a device that does not sync again before
its class never rewrites it. This brings every pending schedule job up to
the new priority once, at deploy.

Revision ID: f3c8a1d5b927
Revises: e7b2c9d41f63
Create Date: 2026-10-04 19:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'f3c8a1d5b927'
down_revision: Union[str, Sequence[str], None] = 'e7b2c9d41f63'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "UPDATE push_jobs SET priority = 10 "
        "WHERE channel = 'schedule' AND status = 'pending' AND priority = 100"
    )


def downgrade() -> None:
    # Nothing to undo: priority 10 is what these jobs are filed at now.
    pass
