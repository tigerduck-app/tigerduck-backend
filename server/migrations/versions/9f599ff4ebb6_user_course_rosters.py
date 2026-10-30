"""user_course_rosters: when the school's roster of each term last arrived
for a user

Revision ID: 9f599ff4ebb6
Revises: 8184b299cf8e
Create Date: 2026-10-30 18:00:00.000000

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '9f599ff4ebb6'
down_revision: str | Sequence[str] | None = '8184b299cf8e'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS user_course_rosters (
            user_id     uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
            semester    varchar(16) NOT NULL,
            received_at timestamptz NOT NULL,
            PRIMARY KEY (user_id, semester)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS user_course_rosters")
