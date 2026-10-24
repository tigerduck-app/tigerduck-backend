"""tls_pin_sets and tls_pin_documents, seeded with the apps' built-in sets

Revision ID: 8996a3bf992f
Revises: c5e8a2f71d40
Create Date: 2026-10-25 12:00:00.000000

"""
import json
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '8996a3bf992f'
down_revision: str | Sequence[str] | None = 'c5e8a2f71d40'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NTUST_HOSTS = [
    f"{name}.ntust.edu.tw"
    for name in (
        "moodle2", "ssoam2", "i", "www", "courseselection", "querycourse",
        "stuinfosys", "cardsystem",
    )
] + ["mail.ntust.edu.tw:993", "mail.ntust.edu.tw:465"]

# The sets both apps build in. The first document is signed from these, and
# its serial becomes the apps' baseline_serial.
_SEED = [
    (
        "ntust",
        "ntust.edu.tw",
        [
            ("TWCA Secure SSL CA", "9VZ7Yd685RTXsE6rL/puuMbnejYaXwaZasGL7c+Uolc="),
            ("TWCA SSL CA", "BxOmElsPeD54MBOb5h+TPNJnBlsAk6spRc/wLnko0cc="),
            ("TWCA Global Root CA", "xES1tmzl1x4bXkDyc4XJXL/SSgW1b3DKwJkvD1DDN5w="),
            ("TWCA CYBER Root CA", "BmALlMAxi7aWfwx3h8yKEDKhecTpXjxXYLMuKQ9/7Js="),
        ],
        datetime(2027, 8, 18, tzinfo=UTC),
        _NTUST_HOSTS,
    ),
    (
        "library",
        "api.lib.ntust.edu.tw",
        [
            ("Sectigo DV E36", "ZSagvDzjltLkewXEBuDxIzpW/dpVw1Juvvmd0hhkzdY="),
            ("Sectigo Root E46", "sLVjNUaFYfW7n6EtgBeEpjOlcnBdNPMrZDRF36iwBdE="),
            ("Sectigo Root R46", "Douxi77vs4G+Ib/BogbTFymEYq0QSFXwSgVCaZcI09Q="),
        ],
        datetime(2027, 5, 4, tzinfo=UTC),
        ["api.lib.ntust.edu.tw"],
    ),
]


def upgrade() -> None:
    """The pin sets the watcher and the portal edit, and every document
    signed from them. Seeds the two sets with `auto_publish` off, so the
    watcher only alerts until an operator turns it on. A second run changes
    nothing.
    """
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS tls_pin_sets (
            id              text PRIMARY KEY,
            domains         jsonb NOT NULL,
            pins            jsonb NOT NULL DEFAULT '[]'::jsonb,
            expires_at      timestamptz NOT NULL,
            monitored_hosts jsonb NOT NULL DEFAULT '[]'::jsonb,
            auto_publish    boolean NOT NULL DEFAULT false,
            watch_results   jsonb NOT NULL DEFAULT '{}'::jsonb,
            updated_at      timestamptz NOT NULL DEFAULT now(),
            updated_by      text
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS tls_pin_documents (
            serial       bigint PRIMARY KEY,
            key_id       text NOT NULL,
            payload      bytea NOT NULL,
            signature    bytea NOT NULL,
            envelope     bytea NOT NULL,
            issued_at    timestamptz NOT NULL,
            expires_at   timestamptz NOT NULL,
            published_at timestamptz NOT NULL DEFAULT now(),
            reason       text NOT NULL
        )
        """
    )
    insert = sa.text(
        "INSERT INTO tls_pin_sets"
        " (id, domains, pins, expires_at, monitored_hosts, auto_publish, updated_by)"
        " VALUES (:id, CAST(:domains AS jsonb), CAST(:pins AS jsonb),"
        " :expires_at, CAST(:hosts AS jsonb), false, 'migration')"
        " ON CONFLICT (id) DO NOTHING"
    )
    for set_id, domain, pins, expires_at, hosts in _SEED:
        op.get_bind().execute(
            insert,
            {
                "id": set_id,
                "domains": json.dumps([{"name": domain, "include_subdomains": True}]),
                "pins": json.dumps(
                    [
                        {
                            "spki": spki,
                            "label": label,
                            "first_seen_at": None,
                            "last_seen_at": None,
                            "source": "builtin",
                        }
                        for label, spki in pins
                    ]
                ),
                "expires_at": expires_at,
                "hosts": json.dumps(hosts),
            },
        )


def downgrade() -> None:
    """The previous release neither reads nor writes them; the documents go
    too, and the apps keep the last one they verified until it expires."""
    op.execute("DROP TABLE IF EXISTS tls_pin_documents")
    op.execute("DROP TABLE IF EXISTS tls_pin_sets")
