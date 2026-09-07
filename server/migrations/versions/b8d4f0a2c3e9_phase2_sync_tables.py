"""phase2 sync tables

Revision ID: b8d4f0a2c3e9
Revises: a7c3f9e1d2b8
Create Date: 2026-06-10 06:16:15.348376

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b8d4f0a2c3e9'
down_revision: Union[str, Sequence[str], None] = 'a7c3f9e1d2b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Phase 2 sync: per-user assignment and bulletin state, subscriptions, and
    the `change_log` that revision-based pull reads from."""
    op.create_table('user_courses',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('semester', sa.String(length=16), nullable=False),
    sa.Column('course_key', sa.String(length=128), nullable=False),
    sa.Column('course_no', sa.String(length=64), nullable=True),
    sa.Column('source', sa.String(length=32), server_default='ntust_portal', nullable=False),
    sa.Column('course_name', sa.String(length=256), nullable=False),
    sa.Column('course_name_en', sa.String(length=256), nullable=True),
    sa.Column('instructors', postgresql.ARRAY(sa.Text()), server_default='{}', nullable=False),
    sa.Column('credits', sa.Numeric(precision=3, scale=1), nullable=True),
    sa.Column('classroom', sa.String(length=128), nullable=True),
    sa.Column('enrolled_count', sa.SmallInteger(), nullable=True),
    sa.Column('max_count', sa.SmallInteger(), nullable=True),
    sa.Column('moodle_id', sa.String(length=64), nullable=True),
    sa.Column('schedule_json', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('classroom_map', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('raw_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('enrollment_status', sa.String(length=32), server_default='enrolled', nullable=False),
    sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'ntust_portal' AND course_no IS NOT NULL) OR source = 'user_added'", name='chk_course_source_key'),
    sa.CheckConstraint("enrollment_status IN ('enrolled', 'dropped', 'completed')", name='chk_enrollment_status'),
    sa.CheckConstraint("source IN ('ntust_portal', 'user_added')", name='chk_course_source'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'semester', 'course_key')
    )
    op.create_index('idx_user_courses_semester', 'user_courses', ['user_id', 'semester'], unique=False, postgresql_where=sa.text('deleted_at IS NULL'))
    op.create_table('user_sync_state',
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('current_revision', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('compacted_revision', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('current_revision >= compacted_revision', name='chk_user_sync_revision'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id')
    )
    op.create_table('user_assignments',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('user_course_id', sa.BigInteger(), nullable=True),
    sa.Column('moodle_course_id', sa.BigInteger(), nullable=False),
    sa.Column('moodle_assignment_id', sa.BigInteger(), nullable=False),
    sa.Column('course_no', sa.String(length=64), nullable=True),
    sa.Column('course_name', sa.String(length=256), nullable=True),
    sa.Column('title', sa.String(length=512), nullable=False),
    sa.Column('due_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cutoff_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('allow_from_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('moodle_url', sa.String(length=512), nullable=True),
    sa.Column('intro_html', sa.Text(), nullable=True),
    sa.Column('provider_is_submitted', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('provider_submitted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('provider_grading_status', sa.String(length=64), nullable=True),
    sa.Column('provider_grade', sa.String(length=64), nullable=True),
    sa.Column('raw_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('fetched_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_course_id'], ['user_courses.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'moodle_course_id', 'moodle_assignment_id')
    )
    op.create_index('idx_user_assignments_active', 'user_assignments', ['user_id'], unique=False, postgresql_where=sa.text('deleted_at IS NULL'))
    op.create_index('idx_user_assignments_course', 'user_assignments', ['user_course_id'], unique=False, postgresql_where=sa.text('deleted_at IS NULL'))
    op.create_index('idx_user_assignments_due', 'user_assignments', ['user_id', 'due_at'], unique=False, postgresql_where=sa.text('deleted_at IS NULL AND provider_is_submitted = false'))
    op.create_table('user_bulletin_states',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('bulletin_id', sa.BigInteger(), nullable=False),
    sa.Column('is_read', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('read_updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('read_device_id', sa.UUID(), nullable=True),
    sa.Column('first_read_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('is_starred', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('starred_updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('starred_device_id', sa.UUID(), nullable=True),
    sa.Column('is_hidden', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('hidden_updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('hidden_device_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['bulletin_id'], ['bulletins.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['hidden_device_id'], ['user_devices.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['read_device_id'], ['user_devices.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['starred_device_id'], ['user_devices.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'bulletin_id')
    )
    op.create_index('idx_bulletin_states_hidden', 'user_bulletin_states', ['user_id', 'hidden_updated_at'], unique=False, postgresql_where=sa.text('is_hidden = true'))
    op.create_index('idx_bulletin_states_starred', 'user_bulletin_states', ['user_id', 'starred_updated_at'], unique=False, postgresql_where=sa.text('is_starred = true'))
    op.create_table('user_bulletin_subscriptions',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=True),
    sa.Column('orgs', postgresql.ARRAY(sa.Text()), server_default='{}', nullable=False),
    sa.Column('tags', postgresql.ARRAY(sa.Text()), server_default='{}', nullable=False),
    sa.Column('mode', sa.String(length=8), server_default='AND', nullable=False),
    sa.Column('enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('revision', sa.BigInteger(), server_default='1', nullable=False),
    sa.Column('created_by_device_id', sa.UUID(), nullable=True),
    sa.Column('updated_by_device_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("mode IN ('AND', 'OR')", name='chk_subscription_mode'),
    sa.CheckConstraint('revision >= 1', name='chk_subscription_revision'),
    sa.ForeignKeyConstraint(['created_by_device_id'], ['user_devices.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['updated_by_device_id'], ['user_devices.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_bulletin_subs_user_active', 'user_bulletin_subscriptions', ['user_id'], unique=False, postgresql_where=sa.text('enabled = true AND deleted_at IS NULL'))
    op.create_table('user_change_log',
    sa.Column('revision', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('entity_type', sa.String(length=64), nullable=False),
    sa.Column('entity_id', sa.String(length=128), nullable=False),
    sa.Column('operation', sa.String(length=16), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('device_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("entity_type IN ('course', 'course_override', 'course_skipped_date', 'assignment', 'assignment_override', 'settings_document', 'bulletin_subscription', 'bulletin_state', 'bulletin_match')", name='chk_changelog_entity_type'),
    sa.CheckConstraint("operation IN ('upsert', 'delete')", name='chk_changelog_operation'),
    sa.ForeignKeyConstraint(['device_id'], ['user_devices.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('revision')
    )
    op.create_index('idx_change_log_created_at', 'user_change_log', ['created_at'], unique=False)
    op.create_index('idx_change_log_user_revision', 'user_change_log', ['user_id', 'revision'], unique=False)
    op.create_table('user_course_overrides',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('user_course_id', sa.BigInteger(), nullable=False),
    sa.Column('custom_name', sa.String(length=256), nullable=True),
    sa.Column('custom_name_updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('custom_name_device_id', sa.UUID(), nullable=True),
    sa.Column('color_hex', sa.String(length=16), nullable=True),
    sa.Column('color_hex_updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('color_hex_device_id', sa.UUID(), nullable=True),
    sa.Column('is_hidden', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('is_hidden_updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('is_hidden_device_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['color_hex_device_id'], ['user_devices.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['custom_name_device_id'], ['user_devices.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['is_hidden_device_id'], ['user_devices.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_course_id'], ['user_courses.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'user_course_id')
    )
    op.create_table('user_course_skipped_dates',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('user_course_id', sa.BigInteger(), nullable=False),
    sa.Column('skipped_on', sa.Date(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('created_by_device_id', sa.UUID(), nullable=True),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('deleted_by_device_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['created_by_device_id'], ['user_devices.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['deleted_by_device_id'], ['user_devices.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_course_id'], ['user_courses.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'user_course_id', 'skipped_on')
    )
    op.create_index('idx_course_skipped_dates_active', 'user_course_skipped_dates', ['user_id', 'skipped_on'], unique=False, postgresql_where=sa.text('deleted_at IS NULL'))
    op.create_table('user_settings_documents',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('namespace', sa.String(length=64), nullable=False),
    sa.Column('schema_version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('document', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('revision', sa.BigInteger(), server_default='1', nullable=False),
    sa.Column('created_by_device_id', sa.UUID(), nullable=True),
    sa.Column('updated_by_device_id', sa.UUID(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("namespace IN ('home_layout', 'appearance', 'assignment_display', 'notification', 'browser', 'language', 'schedule_display', 'watch', 'wearos')", name='chk_settings_namespace'),
    sa.CheckConstraint('revision >= 1', name='chk_settings_revision'),
    sa.CheckConstraint('schema_version >= 1', name='chk_settings_schema_version'),
    sa.ForeignKeyConstraint(['created_by_device_id'], ['user_devices.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['updated_by_device_id'], ['user_devices.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_user_settings_user_updated', 'user_settings_documents', ['user_id', 'updated_at'], unique=False, postgresql_where=sa.text('deleted_at IS NULL'))
    op.create_index('ux_user_settings_namespace_active', 'user_settings_documents', ['user_id', 'namespace'], unique=True, postgresql_where=sa.text('deleted_at IS NULL'))
    op.create_table('bulletin_user_matches',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('bulletin_id', sa.BigInteger(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('subscription_id', sa.BigInteger(), nullable=True),
    sa.Column('match_reason', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('push_job_id', sa.BigInteger(), nullable=True),
    sa.Column('pushed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('matched_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['bulletin_id'], ['bulletins.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['push_job_id'], ['push_jobs.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['subscription_id'], ['user_bulletin_subscriptions.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('bulletin_id', 'user_id')
    )
    op.create_index('idx_bulletin_matches_pending_push', 'bulletin_user_matches', ['pushed_at'], unique=False, postgresql_where=sa.text('pushed_at IS NULL'))
    op.create_index('idx_bulletin_matches_user', 'bulletin_user_matches', ['user_id', sa.literal_column('matched_at DESC')], unique=False)
    op.create_table('user_assignment_overrides',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('user_assignment_id', sa.BigInteger(), nullable=False),
    sa.Column('local_status', sa.String(length=32), server_default='none', nullable=False),
    sa.Column('local_status_updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('local_status_device_id', sa.UUID(), nullable=True),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('note_updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('note_device_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("local_status IN ('none', 'locally_completed', 'ignored', 'archived')", name='chk_local_status'),
    sa.ForeignKeyConstraint(['local_status_device_id'], ['user_devices.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['note_device_id'], ['user_devices.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_assignment_id'], ['user_assignments.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'user_assignment_id')
    )


def downgrade() -> None:
    """Drop the Phase 2 sync tables. All per-user cloud state is lost; clients
    fall back to whatever is in their local cache."""
    op.drop_table('user_assignment_overrides')
    op.drop_index('idx_bulletin_matches_user', table_name='bulletin_user_matches')
    op.drop_index('idx_bulletin_matches_pending_push', table_name='bulletin_user_matches', postgresql_where=sa.text('pushed_at IS NULL'))
    op.drop_table('bulletin_user_matches')
    op.drop_index('ux_user_settings_namespace_active', table_name='user_settings_documents', postgresql_where=sa.text('deleted_at IS NULL'))
    op.drop_index('idx_user_settings_user_updated', table_name='user_settings_documents', postgresql_where=sa.text('deleted_at IS NULL'))
    op.drop_table('user_settings_documents')
    op.drop_index('idx_course_skipped_dates_active', table_name='user_course_skipped_dates', postgresql_where=sa.text('deleted_at IS NULL'))
    op.drop_table('user_course_skipped_dates')
    op.drop_table('user_course_overrides')
    op.drop_index('idx_change_log_user_revision', table_name='user_change_log')
    op.drop_index('idx_change_log_created_at', table_name='user_change_log')
    op.drop_table('user_change_log')
    op.drop_index('idx_bulletin_subs_user_active', table_name='user_bulletin_subscriptions', postgresql_where=sa.text('enabled = true AND deleted_at IS NULL'))
    op.drop_table('user_bulletin_subscriptions')
    op.drop_index('idx_bulletin_states_starred', table_name='user_bulletin_states', postgresql_where=sa.text('is_starred = true'))
    op.drop_index('idx_bulletin_states_hidden', table_name='user_bulletin_states', postgresql_where=sa.text('is_hidden = true'))
    op.drop_table('user_bulletin_states')
    op.drop_index('idx_user_assignments_due', table_name='user_assignments', postgresql_where=sa.text('deleted_at IS NULL AND provider_is_submitted = false'))
    op.drop_index('idx_user_assignments_course', table_name='user_assignments', postgresql_where=sa.text('deleted_at IS NULL'))
    op.drop_index('idx_user_assignments_active', table_name='user_assignments', postgresql_where=sa.text('deleted_at IS NULL'))
    op.drop_table('user_assignments')
    op.drop_table('user_sync_state')
    op.drop_index('idx_user_courses_semester', table_name='user_courses', postgresql_where=sa.text('deleted_at IS NULL'))
    op.drop_table('user_courses')
