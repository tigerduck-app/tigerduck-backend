"""merge_heads

Revision ID: a9f104262be3
Revises: 4e6c604ad58c, b3d5f7a9c1e2
Create Date: 2026-06-20 23:04:54.720721

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a9f104262be3'
down_revision: Union[str, Sequence[str], None] = ('4e6c604ad58c', 'b3d5f7a9c1e2')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
