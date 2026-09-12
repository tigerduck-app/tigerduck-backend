"""per-device bulletin subscriptions

Bulletin subscription rules leave TigerSync and belong to one device each:
a device keeps its own rules, and a matched bulletin is pushed only to the
devices whose rules hit it.

Adds user_bulletin_subscriptions.device_id (FK user_devices.id, ON DELETE
CASCADE). Every active account-level rule is copied onto each of that
account's signed-in devices, so every device keeps receiving exactly the
bulletins it received before; the account-level rows are then removed and
the column made NOT NULL. A rule whose account has no signed-in device
reached nobody, and goes with them.

bulletin_user_matches.subscription_id is ON DELETE SET NULL, so removing the
old rows only forgets which rule an old match came from.

Revision ID: c41d7a9e2f05
Revises: 9e8b6368aa64
Create Date: 2026-09-13 04:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c41d7a9e2f05'
down_revision: Union[str, Sequence[str], None] = '9e8b6368aa64'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'user_bulletin_subscriptions',
        sa.Column(
            'device_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('user_devices.id', ondelete='CASCADE'),
            nullable=True,
        ),
    )
    op.execute(
        """
        INSERT INTO user_bulletin_subscriptions
            (user_id, device_id, name, orgs, tags, mode, enabled, revision,
             created_by_device_id, updated_by_device_id, created_at, updated_at)
        SELECT s.user_id, d.id, s.name, s.orgs, s.tags, s.mode, s.enabled, 1,
               s.created_by_device_id, s.updated_by_device_id,
               s.created_at, s.updated_at
        FROM user_bulletin_subscriptions s
        JOIN user_devices d
          ON d.user_id = s.user_id AND d.deleted_at IS NULL
        WHERE s.device_id IS NULL AND s.deleted_at IS NULL
        """
    )
    op.execute("DELETE FROM user_bulletin_subscriptions WHERE device_id IS NULL")
    op.alter_column('user_bulletin_subscriptions', 'device_id', nullable=False)
    op.create_index(
        'idx_bulletin_subs_device_active',
        'user_bulletin_subscriptions',
        ['device_id'],
        unique=False,
        postgresql_where=sa.text('deleted_at IS NULL'),
    )


def downgrade() -> None:
    """Folds each account's device rules back into account-level rules,
    one per distinct (orgs, tags, mode), then drops the column. Which
    device a rule came from is lost; every rule an account still had on any
    device survives.
    """
    op.drop_index(
        'idx_bulletin_subs_device_active',
        table_name='user_bulletin_subscriptions',
        postgresql_where=sa.text('deleted_at IS NULL'),
    )
    op.alter_column('user_bulletin_subscriptions', 'device_id', nullable=True)
    op.execute(
        """
        INSERT INTO user_bulletin_subscriptions
            (user_id, name, orgs, tags, mode, enabled, revision,
             created_by_device_id, updated_by_device_id, created_at, updated_at)
        SELECT DISTINCT ON (s.user_id, s.orgs, s.tags, s.mode)
               s.user_id, s.name, s.orgs, s.tags, s.mode, s.enabled, 1,
               s.created_by_device_id, s.updated_by_device_id,
               s.created_at, s.updated_at
        FROM user_bulletin_subscriptions s
        WHERE s.device_id IS NOT NULL AND s.deleted_at IS NULL
        ORDER BY s.user_id, s.orgs, s.tags, s.mode, s.updated_at DESC
        """
    )
    op.execute("DELETE FROM user_bulletin_subscriptions WHERE device_id IS NOT NULL")
    op.drop_column('user_bulletin_subscriptions', 'device_id')
