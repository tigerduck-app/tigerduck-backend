"""device model

Adds UserDevice.device_model, the hardware model a device reports at
login and on every register call ("Google Pixel 8", "iPhone17,3"). Shown
in the portal's device Info tab for support work; nothing targets or
gates on it.

Nullable with no backfill: rows predating the column, and clients that
have not shipped the field, simply have no model until their next
register call.

Revision ID: d5a8c3b17e40
Revises: c41d7a9e2f05
Create Date: 2026-09-13 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd5a8c3b17e40'
down_revision: Union[str, Sequence[str], None] = 'c41d7a9e2f05'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('user_devices', sa.Column('device_model', sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column('user_devices', 'device_model')
