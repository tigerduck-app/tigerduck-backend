"""Migrations 8178548f185e (the sync log's key to users), 4f13b23279f1
(device_active_days) and 8184b299cf8e (the index on
auth_sessions.replaced_by_session_id)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from server.tests.conftest import _alembic

pytestmark = pytest.mark.asyncio(loop_scope="session")

REVISION = "8184b299cf8e"
BEFORE = "199cfb4bbb49"

_LOG = "INSERT INTO sync_log_entries (user_id, source, message) VALUES (:u, 's', 'm')"
_STATE = """
SELECT
    (SELECT convalidated FROM pg_constraint WHERE conname = 'sync_log_entries_user_id_fkey'),
    to_regclass('device_active_days') IS NOT NULL,
    (SELECT indisvalid FROM pg_index
      WHERE indexrelid = to_regclass('idx_auth_sessions_replaced_by'))
"""


async def test_upgrade_drops_the_log_of_deleted_accounts_and_survives_a_rerun(
    migration_db_url: str,
) -> None:
    _alembic(migration_db_url, "upgrade", BEFORE)
    engine = create_async_engine(migration_db_url)
    kept, gone = uuid.uuid4(), uuid.uuid4()
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO users (id, student_id) VALUES (:u, 'b1')"), {"u": kept}
            )
            await conn.execute(text(_LOG), {"u": kept})
            await conn.execute(text(_LOG), {"u": gone})

        _alembic(migration_db_url, "upgrade", REVISION)
        async with engine.connect() as conn:
            users = await conn.scalars(text("SELECT user_id FROM sync_log_entries"))
            assert list(users) == [kept]
            assert tuple((await conn.execute(text(_STATE))).one()) == (True, True, True)

        # A rollback that only stamps back keeps the key, the table and the index.
        _alembic(migration_db_url, "stamp", BEFORE)
        _alembic(migration_db_url, "upgrade", REVISION)
        async with engine.begin() as conn:
            assert tuple((await conn.execute(text(_STATE))).one()) == (True, True, True)
            await conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": kept})
            assert await conn.scalar(text("SELECT count(*) FROM sync_log_entries")) == 0

        _alembic(migration_db_url, "downgrade", BEFORE)
        async with engine.begin() as conn:
            await conn.execute(text(_LOG), {"u": gone})
            assert tuple((await conn.execute(text(_STATE))).one()) == (None, False, None)
    finally:
        await engine.dispose()
