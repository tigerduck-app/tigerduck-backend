"""tombstone_deleted_by_reset

Revision ID: e1a9c4b7d0f2
Revises: c8d3f1a7e2b4
Create Date: 2026-09-09 23:20:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e1a9c4b7d0f2'
down_revision: str | Sequence[str] | None = 'c8d3f1a7e2b4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Let a tombstone say whether a reset wrote it.

    A reset and a single-course delete want opposite things from the device
    that performed them. A delete must bind its own author -- the course is
    still on the school's roster, so the next portal refresh on that very
    device would otherwise upload it straight back. A reset must not: the
    resetting device wipes the term precisely so it can replace it with the
    roster the portal returns seconds later, and a tombstone that blocks
    that upload leaves the semester empty.

    Until now the reset resolved this by deleting the tombstones outright,
    which also removed the only thing standing between a device that had not
    yet reconciled and a full resurrection of the roster the user just
    cleared. Marking the row instead keeps the deletion on record for every
    other device while leaving the author free to re-upload.

    Backfilled to false: every tombstone written before this migration came
    from a single-course delete, since a reset left none behind.
    """
    op.add_column(
        "user_course_tombstones",
        sa.Column(
            "deleted_by_reset",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("user_course_tombstones", "deleted_by_reset")
