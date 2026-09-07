"""phase1 identity auth

Revision ID: a7c3f9e1d2b8
Revises: e2a1b7c4d9f3
Create Date: 2026-06-10 05:39:53.442703

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a7c3f9e1d2b8'
down_revision: Union[str, Sequence[str], None] = 'e2a1b7c4d9f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Phase 1 identity: `users`, `auth_sessions`, `push_tokens`, `push_jobs` and
    `push_deliveries`.

    The `ux_*` uniques are all partial, scoped to the active rows. A device
    that re-registers, or a student who returns, therefore does not collide
    with its own retired row."""
    op.create_table('users',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('student_id', sa.String(length=64), nullable=True),
    sa.Column('display_name', sa.String(length=128), nullable=True),
    sa.Column('status', sa.String(length=32), server_default='active', nullable=False),
    sa.Column('locale', sa.String(length=16), server_default='zh-Hant', nullable=True),
    sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('active', 'suspended', 'pending_deletion')", name='chk_users_status'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ux_users_student_id_active', 'users', ['student_id'], unique=True, postgresql_where=sa.text('student_id IS NOT NULL AND deleted_at IS NULL'))
    op.create_table('external_accounts',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('provider', sa.String(length=32), nullable=False),
    sa.Column('external_user_id', sa.String(length=128), nullable=False),
    sa.Column('credential_status', sa.String(length=32), server_default='active', nullable=False),
    sa.Column('last_auth_success_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_auth_failure_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_auth_error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("credential_status IN ('active', 'expired', 'invalid', 'revoked')", name='chk_credential_status'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('provider', 'external_user_id'),
    sa.UniqueConstraint('user_id', 'provider')
    )
    op.create_table('user_devices',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('client_device_id', sa.String(length=128), nullable=False),
    sa.Column('platform', sa.String(length=16), nullable=False),
    sa.Column('device_name', sa.String(length=128), nullable=True),
    sa.Column('app_version', sa.String(length=32), nullable=True),
    sa.Column('os_version', sa.String(length=32), nullable=True),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("platform IN ('ios', 'ipados', 'macos', 'windows', 'watchos', 'wearos', 'android')", name='chk_device_platform'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'client_device_id')
    )
    op.create_table('auth_sessions',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('device_id', sa.UUID(), nullable=True),
    sa.Column('refresh_token_hash', sa.String(length=128), nullable=False),
    sa.Column('issued_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_reason', sa.String(length=32), nullable=True),
    sa.Column('replaced_by_session_id', sa.BigInteger(), nullable=True),
    sa.Column('reuse_detected_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_used_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("revoked_reason IS NULL OR revoked_reason IN ('logout', 'rotated', 'reuse_detected', 'expired', 'admin_revoked', 'credential_revoked')", name='chk_revoked_reason'),
    sa.CheckConstraint('expires_at > issued_at', name='chk_session_time'),
    sa.ForeignKeyConstraint(['device_id'], ['user_devices.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['replaced_by_session_id'], ['auth_sessions.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('refresh_token_hash')
    )
    op.create_index('idx_auth_sessions_user_active', 'auth_sessions', ['user_id'], unique=False, postgresql_where=sa.text('revoked_at IS NULL'))
    op.create_table('device_push_tokens',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('device_id', sa.UUID(), nullable=False),
    sa.Column('provider', sa.String(length=16), nullable=False),
    sa.Column('token_kind', sa.String(length=32), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('token_value', sa.String(length=512), nullable=False),
    sa.Column('bundle_id', sa.String(length=128), nullable=True),
    sa.Column('topic', sa.String(length=160), nullable=True),
    sa.Column('environment', sa.String(length=16), nullable=True),
    sa.Column('scope_key', sa.String(length=160), server_default='', nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('status', sa.String(length=16), server_default='active', nullable=False),
    sa.Column('last_success_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_failure_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_failure_code', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("environment IS NULL OR environment IN ('development', 'production')", name='chk_push_token_env'),
    sa.CheckConstraint("provider IN ('apns', 'fcm')", name='chk_push_token_provider'),
    sa.CheckConstraint("status IN ('active', 'invalidated', 'expired')", name='chk_push_token_status'),
    sa.CheckConstraint("token_kind IN ('standard', 'push_to_start', 'live_activity_update')", name='chk_push_token_kind'),
    sa.ForeignKeyConstraint(['device_id'], ['user_devices.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_push_tokens_device_active', 'device_push_tokens', ['device_id', 'token_kind', 'status'], unique=False)
    op.create_index('idx_push_tokens_expiry', 'device_push_tokens', ['status', 'expires_at'], unique=False, postgresql_where=sa.text("status = 'active'"))
    op.create_index('ux_push_token_active', 'device_push_tokens', ['provider', 'token_kind', 'token_hash', 'scope_key'], unique=True, postgresql_where=sa.text("status = 'active'"))
    op.create_table('external_account_credentials',
    sa.Column('external_account_id', sa.BigInteger(), nullable=False),
    sa.Column('encryption_algorithm', sa.String(length=32), server_default='AES-256-GCM', nullable=False),
    sa.Column('encryption_key_id', sa.String(length=64), nullable=False),
    sa.Column('ciphertext', sa.LargeBinary(), nullable=False),
    sa.Column('nonce', sa.LargeBinary(), nullable=False),
    sa.Column('aad', sa.String(length=256), nullable=False),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('rotated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['external_account_id'], ['external_accounts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('external_account_id')
    )
    op.create_table('push_jobs',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('device_id', sa.UUID(), nullable=True),
    sa.Column('dedupe_key', sa.String(length=256), nullable=False),
    sa.Column('channel', sa.String(length=32), nullable=False),
    sa.Column('scenario', sa.String(length=64), nullable=False),
    sa.Column('fire_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('available_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('priority', sa.Integer(), server_default='100', nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('status', sa.String(length=16), server_default='pending', nullable=False),
    sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('max_attempts', sa.Integer(), server_default='3', nullable=False),
    sa.Column('locked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('locked_by', sa.String(length=128), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("channel IN ('assignment', 'course', 'bulletin', 'system', 'custom')", name='chk_push_job_channel'),
    sa.CheckConstraint("status IN ('pending', 'processing', 'sent', 'partial_failed', 'failed', 'cancelled')", name='chk_push_job_status'),
    sa.CheckConstraint('attempts >= 0 AND max_attempts > 0', name='chk_push_job_attempts'),
    sa.ForeignKeyConstraint(['device_id'], ['user_devices.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_push_jobs_due', 'push_jobs', ['status', 'fire_at', 'available_at', 'priority'], unique=False, postgresql_where=sa.text("status = 'pending'"))
    op.create_index('ux_push_jobs_dedupe_active', 'push_jobs', ['user_id', 'dedupe_key'], unique=True, postgresql_where=sa.text("status IN ('pending', 'processing', 'sent', 'partial_failed')"))
    op.create_table('push_deliveries',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('push_job_id', sa.BigInteger(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('device_id', sa.UUID(), nullable=True),
    sa.Column('push_token_id', sa.BigInteger(), nullable=True),
    sa.Column('provider', sa.String(length=16), nullable=False),
    sa.Column('token_kind', sa.String(length=32), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('scope_key', sa.String(length=160), server_default='', nullable=False),
    sa.Column('status', sa.String(length=16), server_default='pending', nullable=False),
    sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('max_attempts', sa.Integer(), server_default='3', nullable=False),
    sa.Column('provider_message_id', sa.String(length=128), nullable=True),
    sa.Column('failure_code', sa.String(length=64), nullable=True),
    sa.Column('failure_message', sa.Text(), nullable=True),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('next_retry_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("provider IN ('apns', 'fcm')", name='chk_delivery_provider'),
    sa.CheckConstraint("status IN ('pending', 'sent', 'failed', 'skipped')", name='chk_delivery_status'),
    sa.CheckConstraint("token_kind IN ('standard', 'push_to_start', 'live_activity_update')", name='chk_delivery_token_kind'),
    sa.CheckConstraint('attempts >= 0 AND max_attempts > 0', name='chk_delivery_attempts'),
    sa.ForeignKeyConstraint(['device_id'], ['user_devices.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['push_job_id'], ['push_jobs.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['push_token_id'], ['device_push_tokens.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_push_deliveries_pending', 'push_deliveries', ['status', 'next_retry_at'], unique=False, postgresql_where=sa.text("status = 'pending'"))
    op.create_index('ux_push_delivery_job_token', 'push_deliveries', ['push_job_id', 'token_hash', 'token_kind', 'scope_key'], unique=True)


def downgrade() -> None:
    """Drop the Phase 1 identity tables, taking every account and session."""
    op.drop_index('ux_push_delivery_job_token', table_name='push_deliveries')
    op.drop_index('idx_push_deliveries_pending', table_name='push_deliveries', postgresql_where=sa.text("status = 'pending'"))
    op.drop_table('push_deliveries')
    op.drop_index('ux_push_jobs_dedupe_active', table_name='push_jobs', postgresql_where=sa.text("status IN ('pending', 'processing', 'sent', 'partial_failed')"))
    op.drop_index('idx_push_jobs_due', table_name='push_jobs', postgresql_where=sa.text("status = 'pending'"))
    op.drop_table('push_jobs')
    op.drop_table('external_account_credentials')
    op.drop_index('ux_push_token_active', table_name='device_push_tokens', postgresql_where=sa.text("status = 'active'"))
    op.drop_index('idx_push_tokens_expiry', table_name='device_push_tokens', postgresql_where=sa.text("status = 'active'"))
    op.drop_index('idx_push_tokens_device_active', table_name='device_push_tokens')
    op.drop_table('device_push_tokens')
    op.drop_index('idx_auth_sessions_user_active', table_name='auth_sessions', postgresql_where=sa.text('revoked_at IS NULL'))
    op.drop_table('auth_sessions')
    op.drop_table('user_devices')
    op.drop_table('external_accounts')
    op.drop_index('ux_users_student_id_active', table_name='users', postgresql_where=sa.text('student_id IS NOT NULL AND deleted_at IS NULL'))
    op.drop_table('users')
