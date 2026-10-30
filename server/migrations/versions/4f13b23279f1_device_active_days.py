"""device_active_days: the days each device started a session on, kept once
its auth_sessions rows are pruned

Revision ID: 4f13b23279f1
Revises: 8178548f185e
Create Date: 2026-10-30 12:30:00.000000

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '4f13b23279f1'
down_revision: str | Sequence[str] | None = '8178548f185e'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """A second run changes nothing."""
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS device_active_days (
            device_id uuid NOT NULL REFERENCES user_devices (id) ON DELETE CASCADE,
            day       date NOT NULL,
            first_at  timestamptz NOT NULL,
            last_at   timestamptz NOT NULL,
            PRIMARY KEY (device_id, day)
        )
        """
    )


def downgrade() -> None:
    """The days of sessions already pruned go with the table."""
    op.execute("DROP TABLE IF EXISTS device_active_days")
