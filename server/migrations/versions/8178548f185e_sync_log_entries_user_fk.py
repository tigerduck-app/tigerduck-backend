"""sync_log_entries.user_id: a foreign key to users, so deleting an account
deletes its sync log

Revision ID: 8178548f185e
Revises: 199cfb4bbb49
Create Date: 2026-10-30 12:00:00.000000

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '8178548f185e'
down_revision: str | Sequence[str] | None = '199cfb4bbb49'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FK = "sync_log_entries_user_id_fkey"


def upgrade() -> None:
    """A second run changes nothing."""
    # NOT VALID takes its lock on users and sync_log_entries only briefly, and
    # from then on no new row can lose its user.
    op.execute(
        f"ALTER TABLE sync_log_entries DROP CONSTRAINT IF EXISTS {_FK},"
        f" ADD CONSTRAINT {_FK} FOREIGN KEY (user_id)"
        " REFERENCES users (id) ON DELETE CASCADE NOT VALID"
    )
    # Committed first, so validating blocks neither writes nor sign-ins.
    with op.get_context().autocommit_block():
        # The log of accounts deleted before the key existed (tigerduck-backend#90).
        op.execute(
            "DELETE FROM sync_log_entries e"
            " WHERE NOT EXISTS (SELECT 1 FROM users u WHERE u.id = e.user_id)"
        )
        op.execute(f"ALTER TABLE sync_log_entries VALIDATE CONSTRAINT {_FK}")


def downgrade() -> None:
    """Drops the key; the log rows it deleted stay deleted."""
    op.execute(f"ALTER TABLE sync_log_entries DROP CONSTRAINT IF EXISTS {_FK}")
