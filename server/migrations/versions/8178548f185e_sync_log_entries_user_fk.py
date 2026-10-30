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
    # The log of accounts deleted before the key existed (tigerduck-backend#90).
    op.execute(
        "DELETE FROM sync_log_entries e"
        " WHERE NOT EXISTS (SELECT 1 FROM users u WHERE u.id = e.user_id)"
    )
    op.create_foreign_key(
        _FK, "sync_log_entries", "users", ["user_id"], ["id"], ondelete="CASCADE"
    )


def downgrade() -> None:
    """Drops the key; the log rows it deleted stay deleted."""
    op.drop_constraint(_FK, "sync_log_entries", type_="foreignkey")
