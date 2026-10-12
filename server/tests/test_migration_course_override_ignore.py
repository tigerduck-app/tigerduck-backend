"""The ignore-assignments migration, run by Alembic on a database of its own.

The rest of the suite builds its schema with `Base.metadata.create_all`,
which says nothing about the migration production runs at startup
(`entrypoint.sh`) or about its downgrade. Alembic runs in a subprocess
because `server/migrations/env.py` starts its own event loop.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from server.config import Settings
from server.tests.conftest import _admin_url, _drop_create_db, _drop_db

pytestmark = pytest.mark.asyncio(loop_scope="session")

REPO_ROOT = Path(__file__).resolve().parents[2]
REVISION = "ddf188f91a15"
DOWN_REVISION = "f3c8a1d5b927"
NEW_COLUMNS = {
    "ignore_assignments",
    "ignore_assignments_updated_at",
    "ignore_assignments_device_id",
    "moodle_course_id",
}


@pytest_asyncio.fixture(loop_scope="session")
async def migration_db_url(test_settings: Settings) -> AsyncIterator[str]:
    admin_url = _admin_url(test_settings)
    db_name = "tigerduck_test_migrations"
    await _drop_create_db(admin_url, db_name)
    yield (
        make_url(test_settings.database_url)
        .set(database=db_name)
        .render_as_string(hide_password=False)
    )
    await _drop_db(admin_url, db_name)


def _alembic(database_url: str, *args: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env={**os.environ, "TIGERDUCK_DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr


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
