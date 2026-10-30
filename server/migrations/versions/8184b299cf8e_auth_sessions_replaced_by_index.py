"""auth_sessions.replaced_by_session_id gets an index, so deleting a session
finds the rows that point at it without reading the table

Revision ID: 8184b299cf8e
Revises: 4f13b23279f1
Create Date: 2026-10-30 14:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '8184b299cf8e'
down_revision: str | Sequence[str] | None = '4f13b23279f1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "idx_auth_sessions_replaced_by"


def upgrade() -> None:
    """Built without blocking refreshes; a second run changes nothing."""
    with op.get_context().autocommit_block():
        # A build that failed leaves an invalid index, which IF NOT EXISTS keeps.
        invalid = op.get_bind().scalar(
            sa.text("SELECT NOT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:i)"),
            {"i": _INDEX},
        )
        if invalid:
            op.execute(f"DROP INDEX CONCURRENTLY {_INDEX}")
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX}"
            " ON auth_sessions (replaced_by_session_id)"
            " WHERE replaced_by_session_id IS NOT NULL"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
