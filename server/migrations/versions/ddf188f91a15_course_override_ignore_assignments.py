"""Course overrides can ignore every assignment of the course

Revision ID: ddf188f91a15
Revises: f3c8a1d5b927
Create Date: 2026-10-13 02:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'ddf188f91a15'
down_revision: Union[str, Sequence[str], None] = 'f3c8a1d5b927'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Let a user ignore all of a course's assignments, and say which
    Moodle course that is.

    `ignore_assignments` is a merge triplet like `color_hex`: the value, when
    it was set and by which device. While it is true the reminder scan skips
    the course's assignments and cancels what it filed for them. It finds the
    ones the server fetched by `moodle_course_id`, so until a client has sent
    that it skips only the placeholders the apps upload.

    `moodle_course_id` is the course's numeric Moodle id. The override is
    reached by the course's Moodle idnumber, which the assignments the server
    fetches from Moodle do not carry, so the scan needs the number the client
    sends alongside. Nullable: a client may not know it yet.

    Backfilled to false, which is what every existing override meant.
    """
    op.add_column(
        "user_course_overrides",
        sa.Column(
            "ignore_assignments",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "user_course_overrides",
        sa.Column(
            "ignore_assignments_updated_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "user_course_overrides",
        sa.Column("ignore_assignments_device_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "user_course_overrides_ignore_assignments_device_id_fkey",
        "user_course_overrides",
        "user_devices",
        ["ignore_assignments_device_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "user_course_overrides",
        sa.Column("moodle_course_id", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    """Destroys every course's ignore setting and numeric Moodle id. While
    the columns are gone the full sync carries no `ignore_assignments` and
    the apps keep their own setting, but the reminder scan no longer honours
    it; an upgrade adds the columns back as false, the apps take that false
    on their next full sync, and the settings are gone for good.
    """
    op.drop_column("user_course_overrides", "moodle_course_id")
    op.drop_constraint(
        "user_course_overrides_ignore_assignments_device_id_fkey",
        "user_course_overrides",
        type_="foreignkey",
    )
    op.drop_column("user_course_overrides", "ignore_assignments_device_id")
    op.drop_column("user_course_overrides", "ignore_assignments_updated_at")
    op.drop_column("user_course_overrides", "ignore_assignments")
