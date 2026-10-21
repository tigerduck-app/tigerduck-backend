"""The migration that adds `user_devices.session_reauth_pushed_at` and the
index a device's newest session is read by, run by Alembic on a database of
its own the way a deploy runs it."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from server.tests.conftest import _alembic

pytestmark = pytest.mark.asyncio(loop_scope="session")

REVISION = "516f9bad27f0"
BEFORE = "a463951f49a7"

_DEVICE = """
WITH u AS (INSERT INTO users DEFAULT VALUES RETURNING id)
INSERT INTO user_devices (user_id, client_device_id, platform)
SELECT id, 'iphone', 'ios' FROM u
"""


async def _schema(conn: AsyncConnection) -> tuple[bool, bool]:
    column = await conn.scalar(
        text(
            "SELECT count(*) FROM information_schema.columns"
            " WHERE table_name = 'user_devices'"
            " AND column_name = 'session_reauth_pushed_at'"
        )
    )
    index = await conn.scalar(
        text(
            "SELECT count(*) FROM pg_indexes"
            " WHERE indexname = 'idx_auth_sessions_device'"
        )
    )
    return bool(column), bool(index)


async def test_upgrade_adds_both_and_keeps_the_record_over_a_rollback(
    migration_db_url: str,
) -> None:
    _alembic(migration_db_url, "upgrade", BEFORE)
    engine = create_async_engine(migration_db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(_DEVICE))

        _alembic(migration_db_url, "upgrade", REVISION)
        async with engine.begin() as conn:
            assert await _schema(conn) == (True, True)
            # A device from before was never asked.
            assert await conn.scalar(
                text("SELECT session_reauth_pushed_at FROM user_devices")
            ) is None
            await conn.execute(
                text("UPDATE user_devices SET session_reauth_pushed_at = now()")
            )

        # A rollback stamps the database back and leaves both; the next
        # start of this release upgrades over them.
        _alembic(migration_db_url, "stamp", BEFORE)
        _alembic(migration_db_url, "upgrade", REVISION)
        async with engine.connect() as conn:
            assert await conn.scalar(
                text("SELECT session_reauth_pushed_at IS NOT NULL FROM user_devices")
            )

        _alembic(migration_db_url, "downgrade", BEFORE)
        async with engine.connect() as conn:
            assert await _schema(conn) == (False, False)
        # The downgrade, too, runs over a schema it already changed.
        _alembic(migration_db_url, "stamp", REVISION)
        _alembic(migration_db_url, "downgrade", BEFORE)
    finally:
        await engine.dispose()
