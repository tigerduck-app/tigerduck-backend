"""The tombstone `user_added` migration, run by Alembic on a database of its
own the way a deploy runs it (`entrypoint.sh`)."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from server.tests.conftest import _alembic

pytestmark = pytest.mark.asyncio(loop_scope="session")

REVISION = "a463951f49a7"
DOWN_REVISION = "d899fe7d1f79"


async def _tombstones(conn: AsyncConnection) -> list[tuple]:
    columns = set(
        (
            await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name = 'user_course_tombstones'"
                )
            )
        ).scalars()
    )
    picked = "course_key, user_added" if "user_added" in columns else "course_key"
    return [tuple(r) for r in await conn.execute(text(f"SELECT {picked} FROM user_course_tombstones"))]


async def test_existing_tombstones_keep_binding_through_upgrades_and_a_downgrade(
    migration_db_url: str,
) -> None:
    _alembic(migration_db_url, "upgrade", DOWN_REVISION)
    engine = create_async_engine(migration_db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    WITH u AS (INSERT INTO users DEFAULT VALUES RETURNING id)
                    INSERT INTO user_course_tombstones
                        (user_id, course_key, semester, course_no)
                    SELECT id, 'client:1151:GE1010001', '1151', 'GE1010001' FROM u
                    """
                )
            )

        _alembic(migration_db_url, "upgrade", REVISION)
        async with engine.connect() as conn:
            assert await _tombstones(conn) == [("client:1151:GE1010001", False)]

        # A rollback stamps the database back and keeps the column
        # (MIGRATE.md); the next start upgrades over it again.
        _alembic(migration_db_url, "stamp", DOWN_REVISION)
        _alembic(migration_db_url, "upgrade", REVISION)
        async with engine.connect() as conn:
            assert await _tombstones(conn) == [("client:1151:GE1010001", False)]

        _alembic(migration_db_url, "downgrade", DOWN_REVISION)
        _alembic(migration_db_url, "stamp", REVISION)
        _alembic(migration_db_url, "downgrade", DOWN_REVISION)
        async with engine.connect() as conn:
            assert await _tombstones(conn) == [("client:1151:GE1010001",)]
    finally:
        await engine.dispose()
