"""The migrations that add `local_status_received_at` to the assignment
overrides and the retry times to the bulletins, run by Alembic on a
database of its own the way a deploy runs them."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from server.tests.conftest import _alembic

pytestmark = pytest.mark.asyncio(loop_scope="session")

BEFORE = "cc54925b7852"
PREVIOUS_RELEASE = "ddf188f91a15"

_DONE_MARK = """
WITH u AS (INSERT INTO users DEFAULT VALUES RETURNING id),
a AS (
    INSERT INTO user_assignments (user_id, moodle_course_id, moodle_assignment_id, title)
    SELECT id, 1, 1, 'Homework' FROM u
    RETURNING id, user_id
)
INSERT INTO user_assignment_overrides
    (user_id, user_assignment_id, local_status, updated_at)
SELECT user_id, id, 'locally_completed', now() - interval '2 days' FROM a
"""

_NEW_COLUMNS = {
    "user_assignment_overrides": {"local_status_received_at"},
    "bulletins": {"fanout_retry_at", "match_retry_at"},
}


async def _columns(conn: AsyncConnection, table: str) -> set[str]:
    rows = await conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name = :table"
        ),
        {"table": table},
    )
    return set(rows.scalars())


async def _received_lags(conn: AsyncConnection) -> bool:
    return (
        await conn.execute(
            text(
                "SELECT local_status_received_at IS DISTINCT FROM updated_at"
                " FROM user_assignment_overrides"
            )
        )
    ).scalar_one()


async def test_upgrade_backfills_marks_again_after_a_rollback_and_downgrade_drops_the_columns(
    migration_db_url: str,
) -> None:
    _alembic(migration_db_url, "upgrade", BEFORE)
    engine = create_async_engine(migration_db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(_DONE_MARK))

        _alembic(migration_db_url, "upgrade", "head")
        async with engine.begin() as conn:
            for table, columns in _NEW_COLUMNS.items():
                assert columns <= await _columns(conn, table)
            assert not await _received_lags(conn)
            # The previous release, after a rollback by stamp, clears the
            # mark without moving the column.
            await conn.execute(
                text(
                    "UPDATE user_assignment_overrides"
                    " SET local_status = 'none', updated_at = now()"
                )
            )
            assert await _received_lags(conn)

        _alembic(migration_db_url, "stamp", PREVIOUS_RELEASE)
        _alembic(migration_db_url, "upgrade", "head")
        async with engine.begin() as conn:
            assert not await _received_lags(conn)

        _alembic(migration_db_url, "downgrade", BEFORE)
        async with engine.begin() as conn:
            for table, columns in _NEW_COLUMNS.items():
                assert not columns & await _columns(conn, table)
    finally:
        await engine.dispose()
