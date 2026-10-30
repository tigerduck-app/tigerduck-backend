"""Migrations 8178548f185e (the sync log's key to users) and 4f13b23279f1
(device_active_days)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from server.tests.conftest import _alembic

pytestmark = pytest.mark.asyncio(loop_scope="session")

REVISION = "4f13b23279f1"
BEFORE = "199cfb4bbb49"

_LOG = "INSERT INTO sync_log_entries (user_id, source, message) VALUES (:u, 's', 'm')"
_TABLE = "SELECT to_regclass('device_active_days') IS NOT NULL"


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
            assert await conn.scalar(text(_TABLE))

        # A rollback that only stamps back keeps the key and the table.
        _alembic(migration_db_url, "stamp", BEFORE)
        _alembic(migration_db_url, "upgrade", REVISION)
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": kept})
            assert await conn.scalar(text("SELECT count(*) FROM sync_log_entries")) == 0

        _alembic(migration_db_url, "downgrade", BEFORE)
        async with engine.begin() as conn:
            await conn.execute(text(_LOG), {"u": gone})
            assert not await conn.scalar(text(_TABLE))
    finally:
        await engine.dispose()
