"""Anonymous device registrations keep the hash of their device secret

Revision ID: 9aa3f2c27919
Revises: 452fe3a32c93
Create Date: 2026-11-02 11:00:00.000000

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '9aa3f2c27919'
down_revision: str | Sequence[str] | None = '452fe3a32c93'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """The SHA-256 hex digest of the secret `POST /devices/anonymous` binds.

    Existing rows keep none until their app sends one. A second run changes
    nothing.
    """
    op.execute(
        "ALTER TABLE device_registrations"
        " ADD COLUMN IF NOT EXISTS secret_hash varchar(64)"
    )


def downgrade() -> None:
    """The previous release reads no `secret_hash`."""
    op.execute("ALTER TABLE device_registrations DROP COLUMN IF EXISTS secret_hash")
