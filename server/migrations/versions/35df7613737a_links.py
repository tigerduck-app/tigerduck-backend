"""links: the redirects served at /links/{name}

Revision ID: 35df7613737a
Revises: c5e8a2f71d40
Create Date: 2026-10-25 12:00:00.000000

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '35df7613737a'
down_revision: str | Sequence[str] | None = 'c5e8a2f71d40'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Each row is a redirect the apps follow, edited in the portal and
    checked by the backend (`server/links/`). Seeds the academic calendar
    page, which the apps found through r.xinshou.tw before. A second run
    changes nothing, and keeps an edited calendar link as it is.
    """
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS links (
            name                   text PRIMARY KEY,
            target_url             text NOT NULL,
            enabled                boolean NOT NULL DEFAULT true,
            expectations           jsonb NOT NULL DEFAULT '[]'::jsonb,
            check_interval_minutes integer NOT NULL DEFAULT 60,
            last_check_at          timestamptz,
            last_check_ok          boolean,
            last_check_detail      text,
            alert_state            text NOT NULL DEFAULT 'ok',
            updated_at             timestamptz NOT NULL DEFAULT now(),
            updated_by             text NOT NULL
        )
        """
    )
    op.execute(
        """
        INSERT INTO links (name, target_url, expectations, updated_by)
        VALUES (
            'ntust-calendar',
            'https://www.academic.ntust.edu.tw/p/404-1048-78935.php',
            '[{"type": "status", "equals": 200},
              {"type": "final_host", "in": ["www.academic.ntust.edu.tw"]},
              {"type": "href_suffix_count", "suffix": ".ics", "min": 1}]'::jsonb,
            'migration'
        )
        ON CONFLICT (name) DO NOTHING
        """
    )


def downgrade() -> None:
    """The previous release neither reads nor writes it; the apps then
    still reach the calendar through r.xinshou.tw."""
    op.execute("DROP TABLE IF EXISTS links")
