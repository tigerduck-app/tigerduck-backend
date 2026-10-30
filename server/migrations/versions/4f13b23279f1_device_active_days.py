"""device_active_days: the days each device started a session on, kept once
its auth_sessions rows are pruned

Revision ID: 4f13b23279f1
Revises: 8178548f185e
Create Date: 2026-10-30 12:30:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '4f13b23279f1'
down_revision: str | Sequence[str] | None = '8178548f185e'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "device_active_days",
        sa.Column(
            "device_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user_devices.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("first_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    """The days of sessions already pruned go with the table."""
    op.drop_table("device_active_days")
