"""retire_unscoped_schedule_jobs

Revision ID: f2b7d9c4a1e6
Revises: e1a9c4b7d0f2
Create Date: 2026-09-10 12:00:00.000000

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f2b7d9c4a1e6'
down_revision: str | Sequence[str] | None = 'e1a9c4b7d0f2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Cancel the schedule jobs written before jobs were keyed per device.

    Their keys have no device segment and their payloads no activity id, so
    the per-device sweep in /schedule/sync can no longer replace them and
    the pipeline would settle each one as failed/missing_activity_id when
    it fired. Every device re-posts its schedule on its next foreground,
    which recreates whatever is still due under the new keys.
    """
    op.execute(
        """
        UPDATE push_jobs
           SET status = 'cancelled', cancelled_at = now()
         WHERE channel = 'schedule'
           AND status = 'pending'
           AND dedupe_key LIKE 'schedule:%'
           AND payload->>'activity_id' IS NULL
        """
    )


    # Live Activities are ActivityKit's; the register route now refuses an
    # FCM token of either activity kind, and a row that predates the rule
    # would be selected for a schedule job and handed to FCM.
    op.execute(
        """
        UPDATE device_push_tokens
           SET status = 'invalidated'
         WHERE provider = 'fcm'
           AND token_kind <> 'standard'
           AND status = 'active'
        """
    )


def downgrade() -> None:
    # The cancelled rows are not worth reviving: the client re-posts them.
    pass
