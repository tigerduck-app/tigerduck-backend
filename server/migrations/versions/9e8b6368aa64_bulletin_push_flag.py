"""bulletin push flag

Adds UserDevice.bulletin_push_enabled, a device-level toggle read by
exactly one clause in server/push/pipeline.py::_materialize, applied only
when job.channel == "bulletin". It is distinct from server_push_enabled
(operator custom pushes, read by custom_push_targeting.py and the portal
alone): bulletins are part of the always-on essential-info sync, and this
column is what lets a device opt out of them without dropping off the
push server entirely.

NOT NULL with server_default true and no backfill needed: an existing
row, and a preferences PATCH or register call from a pre-v2.1.0 client
that never sends this field, must keep receiving bulletins rather than
silently losing them because a column appeared (see
07b22743e0f1_device_sync_reminder_flags.py for the same reasoning, and
UserDevice.cloud_sync_enabled / sync_courses for the established
pattern).

Revision ID: 9e8b6368aa64
Revises: 07b22743e0f1
Create Date: 2026-09-12 21:55:14.614794

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9e8b6368aa64'
down_revision: Union[str, Sequence[str], None] = '07b22743e0f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add user_devices.bulletin_push_enabled, NOT NULL, server_default
    true. No backfill pass: the server default already makes every
    existing row (and every row a pre-v2.1.0 client's INSERT/UPDATE ever
    touches without knowing this column exists) resolve to true.
    """
    op.add_column('user_devices', sa.Column('bulletin_push_enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False))


def downgrade() -> None:
    """Drops the column. Costs nothing beyond the preference itself --
    every device reverts to "receives bulletins", which is the same
    behaviour every row had before this migration ever ran.
    """
    op.drop_column('user_devices', 'bulletin_push_enabled')
