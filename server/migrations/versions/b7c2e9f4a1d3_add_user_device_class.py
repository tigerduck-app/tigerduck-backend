"""add_user_device_class

Revision ID: b7c2e9f4a1d3
Revises: 9c4d1e2f3a5b
Create Date: 2026-09-08 12:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b7c2e9f4a1d3'
down_revision: str | Sequence[str] | None = '9c4d1e2f3a5b'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Give `user_devices` the form factor its targeting needs.

    `platform` distinguishes ios / ipados / macos but lumps every Android
    device under "android", so operator targeting could address an iPad and
    not an Android tablet. `device_registrations` has carried `device_class`
    since the custom-push work; this brings the signed-in table level with it
    so both halves of a send resolve the same classes.

    Backfilled to '' rather than derived from `platform`, because a row
    written before the app reported a class genuinely does not say which form
    factor it is — an Apple row could be any of three. The targeting query
    treats '' as "match by platform instead", which is what those rows
    already did.
    """
    op.add_column(
        "user_devices",
        sa.Column(
            "device_class",
            sa.String(length=16),
            nullable=False,
            server_default="",
        ),
    )


def downgrade() -> None:
    op.drop_column("user_devices", "device_class")
