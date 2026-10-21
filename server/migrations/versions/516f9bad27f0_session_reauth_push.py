"""Devices record their last session reauth push; sessions are read by device

Revision ID: 516f9bad27f0
Revises: a463951f49a7
Create Date: 2026-10-21 22:00:00.000000

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '516f9bad27f0'
down_revision: str | Sequence[str] | None = 'a463951f49a7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Let push/session_reauth.py ask a device left without a session to
    sign in again at most once every 7 days, and find each device's newest
    session without reading every session.

    `session_reauth_pushed_at` starts null, so a device from before has
    never been asked; the column is added without rewriting the table.
    Building the index blocks writes to auth_sessions while it runs, about
    half a second per million sessions, before the backend starts. A second
    run changes nothing.
    """
    op.execute(
        "ALTER TABLE user_devices"
        " ADD COLUMN IF NOT EXISTS session_reauth_pushed_at TIMESTAMP WITH TIME ZONE"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_auth_sessions_device"
        " ON auth_sessions (device_id, id)"
    )


def downgrade() -> None:
    """The previous release reads neither."""
    op.execute("DROP INDEX IF EXISTS idx_auth_sessions_device")
    op.execute(
        "ALTER TABLE user_devices DROP COLUMN IF EXISTS session_reauth_pushed_at"
    )
