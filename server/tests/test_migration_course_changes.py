"""The migration that adds `course_changes`, run by Alembic on a database of
its own the way a deploy runs it."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from server.tests.conftest import _alembic

pytestmark = pytest.mark.asyncio(loop_scope="session")

REVISION = "ea77513de257"
BEFORE = "8d70b4ee9cc7"

# Columns, constraints and indexes as Postgres reports them.
_SHAPE = text(
    """
    SELECT a.attname || ' ' || format_type(a.atttypid, a.atttypmod)
           || CASE WHEN a.attnotnull THEN ' not null' ELSE '' END
           || coalesce(' default ' || pg_get_expr(d.adbin, d.adrelid), '')
    FROM pg_attribute a
    LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
    WHERE a.attrelid = 'course_changes'::regclass AND a.attnum > 0
      AND NOT a.attisdropped
    UNION ALL
    SELECT conname || ' ' || pg_get_constraintdef(oid) FROM pg_constraint
    WHERE conrelid = 'course_changes'::regclass
    UNION ALL
    SELECT indexdef FROM pg_indexes WHERE tablename = 'course_changes'
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
            assert await conn.scalar(text("SELECT to_regclass('course_changes')")) is None
    finally:
        await engine.dispose()
