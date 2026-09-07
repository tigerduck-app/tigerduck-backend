"""add_course_version_tracking_and_tombstones

Revision ID: b3d5f7a9c1e2
Revises: f7e3a6d9c2b1
Create Date: 2026-06-20 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


# revision identifiers, used by Alembic.
revision: str = 'b3d5f7a9c1e2'
down_revision: Union[str, Sequence[str], None] = 'f7e3a6d9c2b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Version tracking on user_courses
    op.add_column(
        "user_courses",
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column(
        "user_courses",
        sa.Column("updated_by_device_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_user_courses_updated_by_device",
        "user_courses",
        "user_devices",
        ["updated_by_device_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # courses_reset_at on users
    op.add_column(
        "users",
        sa.Column("courses_reset_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Tombstones table
    op.create_table(
        "user_course_tombstones",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("course_key", sa.String(128), nullable=False),
        sa.Column("semester", sa.String(16), nullable=False),
        sa.Column("course_no", sa.String(64), nullable=True),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "deleted_by_device_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user_devices.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_unique_constraint(
        "ux_course_tombstones_key",
        "user_course_tombstones",
        ["user_id", "course_key"],
    )
    op.create_index(
        "ix_course_tombstones_user_deleted",
        "user_course_tombstones",
        ["user_id", "deleted_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_course_tombstones_user_deleted", table_name="user_course_tombstones")
    op.drop_constraint("ux_course_tombstones_key", "user_course_tombstones", type_="unique")
    op.drop_table("user_course_tombstones")
    op.drop_column("users", "courses_reset_at")
    op.drop_constraint("fk_user_courses_updated_by_device", "user_courses", type_="foreignkey")
    op.drop_column("user_courses", "updated_by_device_id")
    op.drop_column("user_courses", "version")
