"""The ignore-assignments migration, run by Alembic on a database of its own
the way a deploy runs it.

The rest of the suite builds its schema with `Base.metadata.create_all`,
which says nothing about the migration production runs at startup
(`entrypoint.sh`) or about its downgrade. Alembic runs in a subprocess
because `server/migrations/env.py` starts its own event loop.
"""

from __future__ import annotations

import time

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from server.tests.conftest import _alembic, _run_alembic

pytestmark = pytest.mark.asyncio(loop_scope="session")

REVISION = "ddf188f91a15"
DOWN_REVISION = "f3c8a1d5b927"
NEW_COLUMNS = {
    "ignore_assignments",
    "ignore_assignments_updated_at",
    "ignore_assignments_device_id",
    "moodle_course_id",
}


async def _override_columns(conn: AsyncConnection) -> set[str]:
    rows = await conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name = 'user_course_overrides'"
        )
    )
    return set(rows.scalars())


async def test_upgrade_adds_the_ignore_columns_and_downgrade_drops_them(
    migration_db_url: str,
) -> None:
    _alembic(migration_db_url, "upgrade", DOWN_REVISION)
    engine = create_async_engine(migration_db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    WITH u AS (INSERT INTO users DEFAULT VALUES RETURNING id),
                    c AS (
                        INSERT INTO user_courses
                            (user_id, semester, course_key, course_no, course_name)
                        SELECT id, '1141', 'CS3001301', 'CS3001301', 'OS' FROM u
                        RETURNING id, user_id
                    )
                    INSERT INTO user_course_overrides
                        (user_id, user_course_id, color_hex)
                    SELECT user_id, id, '#FF8800' FROM c
                    """
                )
            )

        _alembic(migration_db_url, "upgrade", REVISION)
        async with engine.connect() as conn:
            assert NEW_COLUMNS <= await _override_columns(conn)
            row = (
                await conn.execute(
                    text(
                        "SELECT color_hex, ignore_assignments,"
                        " ignore_assignments_updated_at, moodle_course_id"
                        " FROM user_course_overrides"
                    )
                )
            ).one()
            # An override from before the column meant "not ignored".
            assert tuple(row) == ("#FF8800", False, None, None)

        _alembic(migration_db_url, "downgrade", DOWN_REVISION)
        async with engine.connect() as conn:
            assert not NEW_COLUMNS & await _override_columns(conn)
            color = await conn.scalar(text("SELECT color_hex FROM user_course_overrides"))
            assert color == "#FF8800"
    finally:
        await engine.dispose()


async def test_upgrade_after_a_rollback_by_stamp_keeps_the_settings(
    migration_db_url: str,
) -> None:
    """Rolling back to 3.2.0 stamps the database to the previous revision
    and keeps the columns (MIGRATE.md). The next start of this release
    upgrades again: it used to fail on the existing columns, and the
    container restarted in a loop."""
    _alembic(migration_db_url, "upgrade", REVISION)
    engine = create_async_engine(migration_db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    WITH u AS (INSERT INTO users DEFAULT VALUES RETURNING id),
                    c AS (
                        INSERT INTO user_courses
                            (user_id, semester, course_key, course_no, course_name)
                        SELECT id, '1141', 'CS3001301', 'CS3001301', 'OS' FROM u
                        RETURNING id, user_id
                    )
                    INSERT INTO user_course_overrides
                        (user_id, user_course_id, ignore_assignments, moodle_course_id)
                    SELECT user_id, id, true, 4242 FROM c
                    """
                )
            )

        _alembic(migration_db_url, "stamp", DOWN_REVISION)
        _alembic(migration_db_url, "upgrade", REVISION)
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT ignore_assignments, moodle_course_id"
                        " FROM user_course_overrides"
                    )
                )
            ).one()
            assert tuple(row) == (True, 4242)
            foreign_keys = await conn.scalar(
                text(
                    "SELECT count(*) FROM pg_constraint WHERE conname ="
                    " 'user_course_overrides_ignore_assignments_device_id_fkey'"
                )
            )
            assert foreign_keys == 1

        # The downgrade, too, runs over a schema it already changed.
        _alembic(migration_db_url, "downgrade", DOWN_REVISION)
        _alembic(migration_db_url, "stamp", REVISION)
        _alembic(migration_db_url, "downgrade", DOWN_REVISION)
        async with engine.connect() as conn:
            assert not NEW_COLUMNS & await _override_columns(conn)
    finally:
        await engine.dispose()


async def test_upgrade_behind_a_held_lock_gives_up_instead_of_waiting(
    migration_db_url: str,
) -> None:
    """The backend migrates as it starts. Behind a lock another transaction
    holds, such as a backup's, the upgrade used to wait for good; it now
    fails after `lock_timeout` and the restarted container tries again."""
    _alembic(migration_db_url, "upgrade", DOWN_REVISION)
    engine = create_async_engine(migration_db_url)
    try:
        async with engine.connect() as conn:
            await conn.execute(
                text("LOCK TABLE user_course_overrides IN ACCESS SHARE MODE")
            )
            started = time.monotonic()
            result = _run_alembic(migration_db_url, "upgrade", REVISION, timeout=60)
            waited = time.monotonic() - started
            await conn.rollback()
        assert result.returncode != 0
        assert "lock timeout" in result.stderr
        assert waited < 30

        _alembic(migration_db_url, "upgrade", REVISION)
        async with engine.connect() as conn:
            assert NEW_COLUMNS <= await _override_columns(conn)
    finally:
        await engine.dispose()
