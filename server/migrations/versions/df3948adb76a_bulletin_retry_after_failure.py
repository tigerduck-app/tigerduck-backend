"""Bulletins record when a failed fan-out or match may run again

Revision ID: df3948adb76a
Revises: bab460b19cc9
Create Date: 2026-10-13 20:10:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'df3948adb76a'
down_revision: Union[str, Sequence[str], None] = 'bab460b19cc9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Let the bulletins after a failing one into the next batches.

    Each pass picks the oldest waiting bulletins, so one whose anonymous
    fan-out or user match keeps failing held a batch slot on every tick.
    After a failure the pass now skips it until `fanout_retry_at` or
    `match_retry_at`. Both start null, which means no wait; the columns are
    added without rewriting the table, and a second run changes nothing.
    """
    op.execute(
        "ALTER TABLE bulletins"
        " ADD COLUMN IF NOT EXISTS fanout_retry_at TIMESTAMP WITH TIME ZONE,"
        " ADD COLUMN IF NOT EXISTS match_retry_at TIMESTAMP WITH TIME ZONE"
    )


def downgrade() -> None:
    """The previous release retries a failed bulletin on every tick."""
    op.execute(
        "ALTER TABLE bulletins"
        " DROP COLUMN IF EXISTS fanout_retry_at,"
        " DROP COLUMN IF EXISTS match_retry_at"
    )
