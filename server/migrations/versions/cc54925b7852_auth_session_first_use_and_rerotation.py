"""Auth sessions record their first use and a second rotation

Revision ID: cc54925b7852
Revises: ddf188f91a15
Create Date: 2026-10-13 08:30:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'cc54925b7852'
down_revision: Union[str, Sequence[str], None] = 'ddf188f91a15'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_REASONS_BEFORE = (
    "'logout', 'rotated', 'reuse_detected', "
    "'expired', 'admin_revoked', 'credential_revoked'"
)
_REASONS = (
    "'logout', 'rotated', 'superseded', 'reuse_detected', "
    "'expired', 'admin_revoked', 'credential_revoked'"
)


def _replace_reason_check(reasons: str) -> str:
    return (
        " DROP CONSTRAINT IF EXISTS chk_revoked_reason,"
        " ADD CONSTRAINT chk_revoked_reason CHECK"
        f" (revoked_reason IS NULL OR revoked_reason IN ({reasons}))"
    )


def upgrade() -> None:
    """Let a refresh tell a client that lost a rotation reply from a copy.

    `first_used_at` is when the session's access token was first accepted
    or its refresh token rotated. Null on every existing row: nothing
    recorded use before, and a session in use gets it on its next request.

    `rerotated_at` marks a rotated token that was rotated once more, by the
    grace retry or the lost-rotation recovery, so neither happens twice.
    Until now the grace retry used `reuse_detected_at` for that, and those
    rows keep it.

    The revoked reason `superseded` is the successor such a second rotation
    replaces. Postgres adds both columns without rewriting the table; the
    new check reads every row once.

    Runs again over columns that already exist, as after a rollback by
    stamp (MIGRATE.md).
    """
    op.execute(
        "ALTER TABLE auth_sessions"
        " ADD COLUMN IF NOT EXISTS first_used_at TIMESTAMP WITH TIME ZONE,"
        " ADD COLUMN IF NOT EXISTS rerotated_at TIMESTAMP WITH TIME ZONE,"
        + _replace_reason_check(_REASONS)
    )


def downgrade() -> None:
    """Drops what the two columns recorded, after writing what the
    previous release reads in their place. A token rotated a second time
    keeps that in `reuse_detected_at`, where the previous release marks a
    grace retry; without it the token could be rotated again, under that
    release or after an upgrade. A superseded successor becomes `rotated`
    with no successor, which takes the theft path if it is presented.
    """
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_attribute
                WHERE attrelid = 'auth_sessions'::regclass
                    AND attname = 'rerotated_at'
                    AND NOT attisdropped
            ) THEN
                UPDATE auth_sessions SET reuse_detected_at = rerotated_at
                WHERE rerotated_at IS NOT NULL AND reuse_detected_at IS NULL;
            END IF;
        END $$
        """
    )
    op.execute(
        "UPDATE auth_sessions"
        " SET revoked_reason = 'rotated', replaced_by_session_id = NULL"
        " WHERE revoked_reason = 'superseded'"
    )
    op.execute(
        "ALTER TABLE auth_sessions"
        + _replace_reason_check(_REASONS_BEFORE)
        + ","
        " DROP COLUMN IF EXISTS rerotated_at,"
        " DROP COLUMN IF EXISTS first_used_at"
    )
