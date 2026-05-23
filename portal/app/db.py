"""Portal's own SQLite store.

Lives on /data (a docker volume) so it survives container recreate.
Schema is bootstrapped on every startup via `CREATE TABLE IF NOT EXISTS`
— no Alembic for this; the schema is tiny and we never need to evolve
in-place under load.

The admins + audit_log model is documented in docs/portal-design.md.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS admins (
    email TEXT PRIMARY KEY,
    added_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    added_by TEXT NOT NULL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    actor_email TEXT NOT NULL,
    action TEXT NOT NULL,
    detail_json TEXT
);
"""


def init_db(db_path: str, bootstrap_admin: str = "") -> None:
    """Create schema and seed the bootstrap admin on every startup.

    Bootstrap upsert is deliberate: removing the bootstrap admin via the
    UI is allowed (an org might rotate the email) but the next restart
    will put them back. Keeps the "you can't lock yourself out" promise.
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)
        if bootstrap_admin:
            conn.execute(
                """
                INSERT INTO admins (email, added_by, notes)
                VALUES (?, 'bootstrap', 'seeded from TIGERDUCK_PORTAL_BOOTSTRAP_ADMIN on startup')
                ON CONFLICT(email) DO NOTHING
                """,
                (bootstrap_admin,),
            )
        conn.commit()


@contextmanager
def connect(db_path: str) -> Iterator[sqlite3.Connection]:
    """Open a short-lived SQLite connection. Closed by context exit.

    SQLite has no real concurrency story beyond "WAL mode + serialize" —
    portal traffic is tiny, so we just open-close per request and rely
    on the GIL + WAL to keep writes coherent.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # WAL gives readers non-blocking access to the table while a writer
    # is mid-commit. Tiny win at this scale; harmless to enable.
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def log_action(
    db_path: str, actor_email: str, action: str, detail_json: str | None = None
) -> None:
    """Append a row to audit_log. Never raises — auditing must not
    block the action it's recording."""
    try:
        with connect(db_path) as conn:
            conn.execute(
                "INSERT INTO audit_log (actor_email, action, detail_json) VALUES (?, ?, ?)",
                (actor_email, action, detail_json),
            )
    except sqlite3.Error:
        pass
