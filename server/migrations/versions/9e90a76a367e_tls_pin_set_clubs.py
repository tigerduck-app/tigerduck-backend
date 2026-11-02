"""Seed the clubs TLS pin set for clubs.ntust.edu.tw

Revision ID: 9e90a76a367e
Revises: bdeb139d9662
Create Date: 2026-11-02 23:00:00.000000

"""
import json
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '9e90a76a367e'
down_revision: str | Sequence[str] | None = 'bdeb139d9662'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SET_ID = "clubs"
_HOST = "clubs.ntust.edu.tw"
# ISRG's roots, which Let's Encrypt chains end at; the apps build in the same set.
_PINS = [
    ("ISRG Root X1", "C5+lpZ7tcVwmwQIMcRtPbsQtWLABXhQzejna0wHFr8M="),
    ("ISRG Root X2", "diGVwiVYbubAI3RW4hB9xU8e/CH2GnkuvVFZE8zmgzI="),
    ("ISRG Root YR", "fk6IOKit1ild5647BH06ujSIq5XbCgqlbYl6ANhhi88="),
    ("ISRG Root YE", "sCkq5UWXjg+7mKu9lMhhYF5bGLsy7VI/UNW3tccdR7w="),
]
_EXPIRES_AT = datetime(2027, 7, 21, tzinfo=UTC)


def upgrade() -> None:
    """The host serves a Let's Encrypt chain, which the `ntust` set's TWCA
    pins do not cover. Seeded with `auto_publish` off like the first sets,
    so the set reaches the document with the next publish. A second run
    keeps what an operator changed.
    """
    op.get_bind().execute(
        sa.text(
            "INSERT INTO tls_pin_sets"
            " (id, domains, pins, expires_at, monitored_hosts, auto_publish, updated_by)"
            " VALUES (:id, CAST(:domains AS jsonb), CAST(:pins AS jsonb),"
            " :expires_at, CAST(:hosts AS jsonb), false, 'migration')"
            " ON CONFLICT (id) DO NOTHING"
        ),
        {
            "id": _SET_ID,
            "domains": json.dumps([{"name": _HOST, "include_subdomains": False}]),
            "pins": json.dumps(
                [
                    {
                        "spki": spki,
                        "label": label,
                        "first_seen_at": None,
                        "last_seen_at": None,
                        "source": "builtin",
                    }
                    for label, spki in _PINS
                ]
            ),
            "expires_at": _EXPIRES_AT,
            "hosts": json.dumps([_HOST]),
        },
    )


def downgrade() -> None:
    """Deletes the set. A document already signed with it keeps it until
    the next publish."""
    op.execute(f"DELETE FROM tls_pin_sets WHERE id = '{_SET_ID}'")
