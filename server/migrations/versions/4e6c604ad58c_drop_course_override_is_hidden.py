"""drop_course_override_is_hidden + create sync_log_entries

Revision ID: 4e6c604ad58c
Revises: b3c4d5e6f7a8
Create Date: 2026-06-20 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB


# revision identifiers, used by Alembic.
revision: str = '4e6c604ad58c'
down_revision: Union[str, Sequence[str], None] = 'b3c4d5e6f7a8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        "user_course_overrides_is_hidden_device_id_fkey",
        "user_course_overrides",
        type_="foreignkey",
    )
    op.drop_column("user_course_overrides", "is_hidden_device_id")
    op.drop_column("user_course_overrides", "is_hidden_updated_at")
    op.drop_column("user_course_overrides", "is_hidden")

    op.create_table(
        "sync_log_entries",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("device_id", UUID(as_uuid=True), nullable=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("level", sa.String(8), nullable=False, server_default=sa.text("'INFO'")),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("detail", JSONB(), nullable=True),
    )
    op.create_index(
        "idx_sync_log_entries_user",
        "sync_log_entries",
        ["user_id", sa.text("ts DESC")],
    )


def downgrade() -> None:
    op.drop_index("idx_sync_log_entries_user", table_name="sync_log_entries")
    op.drop_table("sync_log_entries")

    op.add_column(
        "user_course_overrides",
        sa.Column("is_hidden", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "user_course_overrides",
        sa.Column("is_hidden_updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "user_course_overrides",
        sa.Column("is_hidden_device_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "user_course_overrides_is_hidden_device_id_fkey",
        "user_course_overrides",
        "user_devices",
        ["is_hidden_device_id"],
        ["id"],
        ondelete="SET NULL",
    )
