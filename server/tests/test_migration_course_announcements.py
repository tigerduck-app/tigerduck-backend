"""The migration that adds the course announcement tables and
`course_changes.source_announcement_id`, run by Alembic on a database of its
own the way a deploy runs it."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from server.tests.conftest import _alembic

pytestmark = pytest.mark.asyncio(loop_scope="session")

REVISION = "22370b5daa3b"
BEFORE = "9e90a76a367e"

_TABLES = (
    "user_moodle_courses",
    "course_announcement_courses",
    "course_announcements",
    "course_announcement_extractions",
    "course_announcement_items",
)

# Columns with their defaults, constraints and indexes as Postgres reports them.
_SHAPE = text(
    """
    SELECT c.relname || ' ' || a.attname || ' ' || format_type(a.atttypid, a.atttypmod)
           || CASE WHEN a.attnotnull THEN ' not null' ELSE '' END
           || coalesce(' default ' || pg_get_expr(d.adbin, d.adrelid), '')
    FROM pg_attribute a
    JOIN pg_class c ON c.oid = a.attrelid
    LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
    WHERE c.relname = ANY(:tables) AND c.relkind = 'r' AND a.attnum > 0
      AND NOT a.attisdropped
    UNION ALL
    SELECT c.relname || ' ' || conname || ' ' || pg_get_constraintdef(p.oid)
    FROM pg_constraint p JOIN pg_class c ON c.oid = p.conrelid
    WHERE c.relname = ANY(:tables)
    UNION ALL
    SELECT indexdef FROM pg_indexes WHERE tablename = ANY(:tables)
    """
).bindparams(tables=[*_TABLES, "course_changes"])

_LEFT = text(
    "SELECT count(*) FROM pg_class WHERE relname = ANY(:tables) AND relkind = 'r'"
).bindparams(tables=list(_TABLES))

_SOURCE_COLUMN = text(
    "SELECT count(*) FROM information_schema.columns"
    " WHERE table_name = 'course_changes' AND column_name = 'source_announcement_id'"
)


async def test_upgrade_builds_the_tables_the_models_declare(
    migration_db_url: str, db_session
) -> None:
    declared = set((await db_session.scalars(_SHAPE)).all())
    assert any("source_announcement_id" in shape for shape in declared)
    assert any("ux_course_announcement_extractions_live" in shape for shape in declared)
    _alembic(migration_db_url, "upgrade", REVISION)
    engine = create_async_engine(migration_db_url)
    try:
        async with engine.connect() as conn:
            assert set((await conn.scalars(_SHAPE)).all()) == declared

        # A second run, after a rollback that kept the tables, changes nothing.
        _alembic(migration_db_url, "stamp", BEFORE)
        _alembic(migration_db_url, "upgrade", REVISION)
        async with engine.connect() as conn:
            assert set((await conn.scalars(_SHAPE)).all()) == declared

        _alembic(migration_db_url, "downgrade", BEFORE)
        async with engine.connect() as conn:
            assert await conn.scalar(_LEFT) == 0
            assert await conn.scalar(_SOURCE_COLUMN) == 0
    finally:
        await engine.dispose()


async def test_one_unfinished_extraction_per_version(db_session) -> None:
    await db_session.execute(
        text(
            "INSERT INTO course_announcements (id, term_code, moodle_course_id, subject,"
            " posted_at, modified_at, group_id, has_attachment)"
            " VALUES ('1-2', '1151', 7, 'Invented', now(), now(), -1, false)"
        )
    )
    version = await db_session.scalar(
        text("SELECT modified_at FROM course_announcements WHERE id = '1-2'")
    )
    insert = text(
        "INSERT INTO course_announcement_extractions (announcement_id, version, status)"
        " VALUES ('1-2', :version, :status)"
        " ON CONFLICT (announcement_id, version) WHERE status IN ('queued', 'running')"
        " DO NOTHING RETURNING id"
    )
    assert await db_session.scalar(insert, {"version": version, "status": "queued"})
    assert await db_session.scalar(insert, {"version": version, "status": "queued"}) is None
    assert await db_session.scalar(insert, {"version": version, "status": "done"})
    assert await db_session.scalar(insert, {"version": version, "status": "failed"})
    await db_session.rollback()
