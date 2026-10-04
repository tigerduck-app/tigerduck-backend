"""push_jobs stale-lock index

Indexes the jobs in `processing` by `locked_at`, for the stale-lock sweep
that opens every push tick. The tick now runs every 5s, and push_jobs
keeps 7 days of rows, nearly all of them finished; without this the sweep
read every active row each time to find the few stuck mid-delivery.

Revision ID: e7b2c9d41f63
Revises: d5a8c3b17e40
Create Date: 2026-10-04 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e7b2c9d41f63'
down_revision: Union[str, Sequence[str], None] = 'd5a8c3b17e40'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        'idx_push_jobs_stale_lock',
        'push_jobs',
        ['locked_at'],
        unique=False,
        postgresql_where=sa.text("status = 'processing'"),
    )


def downgrade() -> None:
    op.drop_index('idx_push_jobs_stale_lock', table_name='push_jobs')
