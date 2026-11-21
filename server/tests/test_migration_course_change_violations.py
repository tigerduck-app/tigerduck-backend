"""The migration that adds `course_change_violations` and `term_courses`, run
by Alembic on a database of its own the way a deploy runs it."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from server.tests.conftest import _alembic

pytestmark = pytest.mark.asyncio(loop_scope="session")

REVISION = "bbb33844e87e"
BEFORE = "9e90a76a367e"

_DEFINITIONS = text(
    "SELECT pg_get_functiondef(oid) FROM pg_proc WHERE proname = 'course_change_violations'"
    " UNION ALL SELECT pg_get_viewdef(oid) FROM pg_class"
    " WHERE relname = 'term_courses' AND relkind = 'v'"
)
# fefd80be557b changes the function's clash rule; until then it is this
# revision's, the model's with a change clashing with its own kind alone.
_CLASH = (
    "c.course_no = course\n"
    "         AND (c.kind = change_kind\n"
    "              OR (c.kind IN ('room', 'online') AND change_kind IN ('room', 'online')))\n"
)


def _then(definitions) -> set[str]:
    return {
        d.replace(_CLASH, "c.course_no = course AND c.kind = change_kind\n")
        for d in definitions
    }


async def test_upgrade_builds_the_function_and_view_the_model_declares(
    migration_db_url: str, db_session
) -> None:
    declared = _then((await db_session.scalars(_DEFINITIONS)).all())
    assert len(declared) == 2
    assert not any("'online'" in definition for definition in declared)
    _alembic(migration_db_url, "upgrade", REVISION)
    engine = create_async_engine(migration_db_url)
    try:
        async with engine.connect() as conn:
            assert set((await conn.scalars(_DEFINITIONS)).all()) == declared

        # A second run, after a rollback that kept them, changes nothing.
        _alembic(migration_db_url, "stamp", BEFORE)
        _alembic(migration_db_url, "upgrade", REVISION)
        async with engine.connect() as conn:
            assert set((await conn.scalars(_DEFINITIONS)).all()) == declared

        _alembic(migration_db_url, "downgrade", BEFORE)
        async with engine.connect() as conn:
            assert (await conn.scalars(_DEFINITIONS)).all() == []
    finally:
        await engine.dispose()
