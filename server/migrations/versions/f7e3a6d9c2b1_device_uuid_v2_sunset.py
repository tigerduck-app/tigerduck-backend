"""device_uuid_v2_sunset

Revision ID: f7e3a6d9c2b1
Revises: db27e6d1b65a
Create Date: 2026-06-18 04:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f7e3a6d9c2b1'
down_revision: Union[str, Sequence[str], None] = 'db27e6d1b65a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Despite the revision name, this does not sunset anything: it adds
    `user_devices.server_push_enabled` (default true) and widens two CHECK
    constraints — `push_jobs.channel` to accept 'schedule', and
    `user_devices.platform` to accept 'web'."""
    op.add_column(
        "user_devices",
        sa.Column(
            "server_push_enabled",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
    )
    op.drop_constraint("chk_push_job_channel", "push_jobs", type_="check")
    op.create_check_constraint(
        "chk_push_job_channel",
        "push_jobs",
        "channel IN ('assignment', 'course', 'bulletin', 'system', 'custom', 'schedule')",
    )
    op.drop_constraint("chk_device_platform", "user_devices", type_="check")
    op.create_check_constraint(
        "chk_device_platform",
        "user_devices",
        "platform IN ('ios', 'ipados', 'macos', 'windows', 'watchos', 'wearos', 'android', 'web')",
    )


def downgrade() -> None:
    """Narrow both CHECKs back and drop `server_push_enabled`. Fails if any row
    already uses the 'schedule' channel or the 'web' platform."""
    op.drop_constraint("chk_push_job_channel", "push_jobs", type_="check")
    op.create_check_constraint(
        "chk_push_job_channel",
        "push_jobs",
        "channel IN ('assignment', 'course', 'bulletin', 'system', 'custom')",
    )
    op.drop_constraint("chk_device_platform", "user_devices", type_="check")
    op.create_check_constraint(
        "chk_device_platform",
        "user_devices",
        "platform IN ('ios', 'ipados', 'macos', 'windows', 'watchos', 'wearos', 'android')",
    )
    op.drop_column("user_devices", "server_push_enabled")
