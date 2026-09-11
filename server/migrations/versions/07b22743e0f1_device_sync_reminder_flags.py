"""device sync reminder flags

Adds two device-level toggles gating spec §4.6's "Sync content" switches:
UserDevice.sync_assignment_reminders and UserDevice.sync_live_activity.
Later work gates assignment-reminder / Live Activity push delivery on
these; this migration only adds the columns.

Both are NOT NULL with server_default true and need no backfill: an
existing row, and a preferences PATCH from a pre-v2.1.0 client that never
sends these fields, must keep getting reminders rather than silently lose
them because a column appeared (see UserDevice.cloud_sync_enabled /
sync_courses for the established pattern this follows).

Revision ID: 07b22743e0f1
Revises: c1e7a94b8f20
Create Date: 2026-09-11 15:03:47.931140

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '07b22743e0f1'
down_revision: Union[str, Sequence[str], None] = 'c1e7a94b8f20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('user_devices', sa.Column('sync_assignment_reminders', sa.Boolean(), server_default=sa.text('true'), nullable=False))
    op.add_column('user_devices', sa.Column('sync_live_activity', sa.Boolean(), server_default=sa.text('true'), nullable=False))


def downgrade() -> None:
    op.drop_column('user_devices', 'sync_live_activity')
    op.drop_column('user_devices', 'sync_assignment_reminders')
