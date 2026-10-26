"""The course membership stamp migration, run by Alembic on a database of its
own the way a deploy runs it (`entrypoint.sh`)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from server.tests.conftest import _alembic

pytestmark = pytest.mark.asyncio(loop_scope="session")

REVISION = "8d70b4ee9cc7"
DOWN_REVISION = "88dbb93dba99"
T0 = datetime(2026, 9, 1, tzinfo=UTC)


def at(days: int) -> datetime:
    return T0 + timedelta(days=days)


async def _seed(conn: AsyncConnection) -> None:
    await conn.execute(
        text(
            """
            WITH u AS (INSERT INTO users DEFAULT VALUES RETURNING id),
            c AS (
                INSERT INTO user_courses
                    (user_id, semester, course_key, course_no, course_name, source, created_at)
                SELECT id, '1141', 'client:1141:' || n, n, n, s, :t0
                FROM u, (VALUES ('CS1', 'user_added'), ('CS2', 'ntust_portal')) AS v(n, s)
            )
            INSERT INTO user_course_tombstones
                (user_id, course_key, semester, course_no, deleted_at, deleted_by_reset)
            SELECT id, 'client:' || t || ':' || n, t, n, d, r
            FROM u, (VALUES
                ('1141', 'EE1', CAST(:t1 AS timestamptz), true),
                ('1141', 'EE2', CAST(:t2 AS timestamptz), true),
                ('1142', 'EE3', CAST(:t1 AS timestamptz), true),
                ('1142', 'EE4', CAST(:t3 AS timestamptz), false)
            ) AS v(t, n, d, r)
            """
        ),
        {"t0": T0, "t1": at(1), "t2": at(2), "t3": at(3)},
    )


async def _added(conn: AsyncConnection) -> dict[str, datetime | None]:
    rows = await conn.execute(text("SELECT course_no, added_at FROM user_courses"))
    return dict(rows.all())


async def _resets(conn: AsyncConnection) -> dict[str, datetime]:
    rows = await conn.execute(text("SELECT semester, reset_at FROM user_semester_resets"))
    return dict(rows.all())


async def test_adds_and_resets_take_their_stored_times(migration_db_url: str) -> None:
    _alembic(migration_db_url, "upgrade", DOWN_REVISION)
    engine = create_async_engine(migration_db_url)
    try:
        async with engine.begin() as conn:
            await _seed(conn)

        _alembic(migration_db_url, "upgrade", REVISION)
        async with engine.connect() as conn:
            # Only a course added by hand has an add; a hide is no reset.
            assert await _added(conn) == {"CS1": T0, "CS2": None}
            assert await _resets(conn) == {"1141": at(2), "1142": at(1)}

        # A rollback stamps the database back and keeps both (MIGRATE.md);
        # the previous release adds a course and resets 1141 meanwhile.
        _alembic(migration_db_url, "stamp", DOWN_REVISION)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO user_courses (user_id, semester, course_key, course_no,"
                    " course_name, source, created_at)"
                    " SELECT id, '1141', 'client:1141:CS3', 'CS3', 'CS3', 'user_added', :t4"
                    " FROM users"
                ),
                {"t4": at(4)},
            )
            await conn.execute(
                text(
                    "UPDATE user_course_tombstones SET deleted_at = :t5"
                    " WHERE course_no = 'EE1'"
                ),
                {"t5": at(5)},
            )
        _alembic(migration_db_url, "upgrade", REVISION)
        async with engine.connect() as conn:
            assert await _added(conn) == {"CS1": T0, "CS2": None, "CS3": at(4)}
            assert await _resets(conn) == {"1141": at(5), "1142": at(1)}

        # A removal kept beside a roster row would hide the course in the
        # previous release; one with no row stays.
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO user_course_tombstones"
                    " (user_id, course_key, semester, course_no, deleted_at, user_added)"
                    " SELECT id, k, '1141', k, :t6, true FROM users,"
                    " (VALUES ('client:1141:CS2'), ('client:1141:CS9')) AS v(k)"
                ),
                {"t6": at(6)},
            )
        _alembic(migration_db_url, "downgrade", DOWN_REVISION)
        async with engine.connect() as conn:
            kept = await conn.execute(
                text("SELECT course_key FROM user_course_tombstones WHERE user_added")
            )
            assert kept.scalars().all() == ["client:1141:CS9"]
            gone = await conn.execute(
                text(
                    "SELECT to_regclass('user_semester_resets') IS NULL, count(*) = 0"
                    " FROM information_schema.columns"
                    " WHERE table_name = 'user_courses' AND column_name = 'added_at'"
                )
            )
            assert gone.one() == (True, True)
    finally:
        await engine.dispose()
