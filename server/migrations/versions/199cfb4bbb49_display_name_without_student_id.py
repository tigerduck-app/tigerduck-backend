"""users.display_name: the name alone, without the student ID before it

Revision ID: 199cfb4bbb49
Revises: ea77513de257
Create Date: 2026-10-29 20:00:00.000000

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '199cfb4bbb49'
down_revision: str | Sequence[str] | None = 'ea77513de257'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 3.8.0 stored Moodle's full name, "student ID @ name", whole; the name is the
# part after the first `@`, as `display_name_from` in server/auth/moodle.py takes it.
_NAME = "btrim(substr(display_name, strpos(display_name, '@') + 1))"


def upgrade() -> None:
    op.execute(
        f"UPDATE users SET display_name = {_NAME}"
        f" WHERE strpos(display_name, '@') > 0 AND {_NAME} <> ''"
    )


def downgrade() -> None:
    """Nothing to restore: the previous release stores the full name again
    at the user's next sign-in or sync."""
