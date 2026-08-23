"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

# revision identifiers, used by Alembic.
revision: str = ${repr(up_revision)}
down_revision: Union[str, Sequence[str], None] = ${repr(down_revision)}
branch_labels: Union[str, Sequence[str], None] = ${repr(branch_labels)}
depends_on: Union[str, Sequence[str], None] = ${repr(depends_on)}


def upgrade() -> None:
    """TODO: what this changes, and why. "Upgrade schema" is what the
    signature already says — describe the intent, and call out anything a
    reviewer could not infer from the ops below (backfills, partial
    indexes, data that becomes unrecoverable).
    """
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """TODO: what reverting costs. Say plainly if it destroys data or
    fails against rows the upgrade made possible.
    """
    ${downgrades if downgrades else "pass"}
