"""Settings documents admit the club_subscriptions namespace

Revision ID: bdeb139d9662
Revises: 9aa3f2c27919
Create Date: 2026-11-02 22:30:00.000000

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'bdeb139d9662'
down_revision: str | Sequence[str] | None = '9aa3f2c27919'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAMESPACES_BEFORE = (
    "'home_layout', 'appearance', 'assignment_display', "
    "'notification', 'browser', 'language', "
    "'schedule_display', 'watch', 'wearos'"
)
_NAMESPACES = _NAMESPACES_BEFORE + ", 'club_subscriptions'"


def _replace_namespace_check(namespaces: str) -> str:
    return (
        "ALTER TABLE user_settings_documents"
        " DROP CONSTRAINT IF EXISTS chk_settings_namespace,"
        f" ADD CONSTRAINT chk_settings_namespace CHECK (namespace IN ({namespaces}))"
    )


def upgrade() -> None:
    """The class table's club subscriptions, a document the apps write.

    The new check reads every row once. A second run changes nothing, as
    after a rollback by stamp (MIGRATE.md).
    """
    op.execute(_replace_namespace_check(_NAMESPACES))


def downgrade() -> None:
    """Deletes every club_subscriptions document, which the narrowed check
    would refuse; a rollback by stamp keeps them."""
    op.execute(
        "DELETE FROM user_settings_documents WHERE namespace = 'club_subscriptions'"
    )
    op.execute(_replace_namespace_check(_NAMESPACES_BEFORE))
