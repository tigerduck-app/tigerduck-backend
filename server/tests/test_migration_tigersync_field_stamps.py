"""The override stamp migration, run by Alembic on a database of its own the
way a deploy runs it (`entrypoint.sh`)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from server.sync.merge import to_clock
from server.tests.conftest import _alembic

pytestmark = pytest.mark.asyncio(loop_scope="session")

REVISION = "88dbb93dba99"
DOWN_REVISION = "5d2a9c7e4f1b"
T0 = datetime(2026, 9, 1, tzinfo=UTC)
NAMED = T0 + timedelta(days=1)
LATER = T0 + timedelta(days=2)


async def _seed(conn: AsyncConnection) -> None:
    await conn.execute(
        text(
            """
            WITH u AS (INSERT INTO users DEFAULT VALUES RETURNING id),
            c AS (
                INSERT INTO user_courses (user_id, semester, course_key, course_no, course_name)
                SELECT id, '1141', 'client:1141:' || n, n, n
                FROM u, (VALUES ('CS1'), ('CS2')) AS v(n)
                RETURNING id, user_id, course_no
            ),
            a AS (
                INSERT INTO user_assignments
                    (user_id, moodle_course_id, moodle_assignment_id, title)
                SELECT id, 7001, n, 'HW' FROM u, (VALUES (1), (2)) AS v(n)
                RETURNING id, user_id, moodle_assignment_id
            ),
            co AS (
                INSERT INTO user_course_overrides
                    (user_id, user_course_id, color_hex, custom_names,
                     custom_name_updated_at, ignore_assignments, updated_at)
                SELECT user_id, id,
                       CASE course_no WHEN 'CS1' THEN '#FF0000' END,
                       CASE course_no WHEN 'CS1' THEN '{"zh": "作業", "en": "HW"}'::jsonb
                                      ELSE '{}'::jsonb END,
                       CASE course_no WHEN 'CS1' THEN CAST(:named AS timestamptz) END,
                       course_no = 'CS1', :t0
                FROM c
            )
            INSERT INTO user_assignment_overrides
                (user_id, user_assignment_id, local_status, note, updated_at)
            SELECT user_id, id,
                   CASE moodle_assignment_id WHEN 1 THEN 'ignored' ELSE 'none' END,
                   CASE moodle_assignment_id WHEN 1 THEN 'calculator' END, :t0
            FROM a
            """
        ),
        {"t0": T0, "named": NAMED},
    )


async def _course_stamps(conn: AsyncConnection) -> dict[str, tuple]:
    rows = await conn.execute(
        text(
            "SELECT c.course_no, o.color_hex_updated_at, o.custom_name_updated_at,"
            " o.ignore_assignments_updated_at, o.custom_names_updated_at, o.updated_at"
            " FROM user_course_overrides o JOIN user_courses c ON c.id = o.user_course_id"
        )
    )
    return {r[0]: tuple(r[1:]) for r in rows}


async def _assignment_stamps(conn: AsyncConnection) -> dict[int, tuple]:
    rows = await conn.execute(
        text(
            "SELECT a.moodle_assignment_id, o.local_status_updated_at, o.note_updated_at"
            " FROM user_assignment_overrides o"
            " JOIN user_assignments a ON a.id = o.user_assignment_id"
        )
    )
    return {r[0]: tuple(r[1:]) for r in rows}


async def test_values_without_a_stamp_take_their_rows_time(migration_db_url: str) -> None:
    _alembic(migration_db_url, "upgrade", DOWN_REVISION)
    engine = create_async_engine(migration_db_url)
    try:
        async with engine.begin() as conn:
            await _seed(conn)

        _alembic(migration_db_url, "upgrade", REVISION)
        names = {"zh": to_clock(NAMED), "en": to_clock(NAMED)}
        async with engine.connect() as conn:
            assert await _course_stamps(conn) == {
                "CS1": (T0, NAMED, T0, names, T0),
                # No value, so nothing to stamp.
                "CS2": (None, None, None, {}, T0),
            }
            assert await _assignment_stamps(conn) == {1: (T0, T0), 2: (None, None)}

        # A rollback stamps the database back and keeps the column
        # (MIGRATE.md); the previous release renames the course meanwhile,
        # and the next start upgrades over it again.
        _alembic(migration_db_url, "stamp", DOWN_REVISION)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE user_course_overrides SET custom_name_updated_at = :later"
                    " WHERE custom_names <> '{}'::jsonb"
                ),
                {"later": LATER},
            )
        _alembic(migration_db_url, "upgrade", REVISION)
        async with engine.connect() as conn:
            stamps = await _course_stamps(conn)
            assert stamps["CS1"][3] == {"zh": to_clock(LATER), "en": to_clock(LATER)}
            assert stamps["CS1"][0] == T0

        _alembic(migration_db_url, "downgrade", DOWN_REVISION)
        async with engine.connect() as conn:
            columns = set(
                (
                    await conn.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns"
                            " WHERE table_name = 'user_course_overrides'"
                        )
                    )
                ).scalars()
            )
            assert "custom_names_updated_at" not in columns
            assert await _assignment_stamps(conn) == {1: (T0, T0), 2: (None, None)}
    finally:
        await engine.dispose()
