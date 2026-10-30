"""The migration that adds `user_course_rosters`, run by Alembic on a database
of its own the way a deploy runs it."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from server.tests.conftest import _alembic

pytestmark = pytest.mark.asyncio(loop_scope="session")

REVISION = "9f599ff4ebb6"
BEFORE = "8184b299cf8e"

# Columns, constraints and indexes as Postgres reports them.
_SHAPE = text(
    """
    SELECT a.attname || ' ' || format_type(a.atttypid, a.atttypmod)
           || CASE WHEN a.attnotnull THEN ' not null' ELSE '' END
    FROM pg_attribute a
    WHERE a.attrelid = 'user_course_rosters'::regclass AND a.attnum > 0
      AND NOT a.attisdropped
    UNION ALL
    SELECT conname || ' ' || pg_get_constraintdef(oid) FROM pg_constraint
    WHERE conrelid = 'user_course_rosters'::regclass
    UNION ALL
    SELECT indexdef FROM pg_indexes WHERE tablename = 'user_course_rosters'
    """
)


async def test_upgrade_builds_the_table_the_model_declares(
    migration_db_url: str, db_session
) -> None:
    declared = set((await db_session.scalars(_SHAPE)).all())
    _alembic(migration_db_url, "upgrade", REVISION)
    engine = create_async_engine(migration_db_url)
    try:
        async with engine.connect() as conn:
            assert set((await conn.scalars(_SHAPE)).all()) == declared

        # A second run, after a rollback that kept the table, changes nothing.
        _alembic(migration_db_url, "stamp", BEFORE)
        _alembic(migration_db_url, "upgrade", REVISION)
        async with engine.connect() as conn:
            assert set((await conn.scalars(_SHAPE)).all()) == declared

        _alembic(migration_db_url, "downgrade", BEFORE)
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT to_regclass('user_course_rosters')")) is None
    finally:
        await engine.dispose()
