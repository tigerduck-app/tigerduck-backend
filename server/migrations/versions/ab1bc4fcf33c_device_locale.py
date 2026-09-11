"""device locale

Adds UserDevice.locale (BCP-47 tag, nullable) so registration can report
the device's language. Nullable and no backfill: existing rows simply
have no reported language yet, and the send path falls back to English.

Revision ID: ab1bc4fcf33c
Revises: f2b7d9c4a1e6
Create Date: 2026-09-11 08:29:45.700665

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'ab1bc4fcf33c'
down_revision: Union[str, Sequence[str], None] = 'f2b7d9c4a1e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "user_devices",
        sa.Column("locale", sa.String(length=35), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_devices", "locale")
