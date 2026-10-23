"""admin_audit_log: who did what in the portal

Revision ID: d07bc39dde6c
Revises: 516f9bad27f0
Create Date: 2026-10-23 12:00:00.000000

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd07bc39dde6c'
down_revision: str | Sequence[str] | None = '516f9bad27f0'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """The portal records each state change and each look at one account
    here (`portal/app/audit.py`); the backend prunes it after
    `admin_audit_retention_days`. No foreign keys, so a record outlives the
    account it names. A second run changes nothing.
    """
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_audit_log (
            id               bigserial PRIMARY KEY,
            ts               timestamptz NOT NULL DEFAULT now(),
            actor            text NOT NULL,
            action           text NOT NULL,
            target_user_id   uuid,
            target_device_id uuid,
            detail           jsonb NOT NULL DEFAULT '{}'::jsonb,
            ip               inet
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_admin_audit_ts"
        " ON admin_audit_log (ts DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_admin_audit_target_user"
        " ON admin_audit_log (target_user_id, ts DESC)"
        " WHERE target_user_id IS NOT NULL"
    )


def downgrade() -> None:
    """The previous release neither reads nor writes it; its rows go too."""
    op.execute("DROP TABLE IF EXISTS admin_audit_log")
