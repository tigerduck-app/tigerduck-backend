"""Bulletin subscriptions carry the client's own id for each rule

Revision ID: 452fe3a32c93
Revises: 9f599ff4ebb6
Create Date: 2026-10-31 20:00:00.000000

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '452fe3a32c93'
down_revision: str | Sequence[str] | None = '9f599ff4ebb6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ux_bulletin_subs_device_client"


def upgrade() -> None:
    """`/v4`'s subscription PUT updates the rule a `client_id` names in place.

    The volatile default gives every existing row its own id as the column
    is added, and every insert that names none, the `/v3` PUT's and the
    per-rule POST's, one too. A second run changes nothing.
    """
    op.execute(
        "ALTER TABLE user_bulletin_subscriptions"
        " ADD COLUMN IF NOT EXISTS client_id uuid NOT NULL DEFAULT gen_random_uuid()"
    )
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {_INDEX}"
        " ON user_bulletin_subscriptions (device_id, client_id)"
        " WHERE deleted_at IS NULL"
    )


def downgrade() -> None:
    """The previous release reads no `client_id`."""
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
    op.execute("ALTER TABLE user_bulletin_subscriptions DROP COLUMN IF EXISTS client_id")
