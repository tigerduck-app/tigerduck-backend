"""The migration that adds `course_change_violations`, run by Alembic on a
database of its own the way a deploy runs it."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from server.tests.conftest import _alembic

pytestmark = pytest.mark.asyncio(loop_scope="session")

REVISION = "bbb33844e87e"
BEFORE = "9e90a76a367e"

_FUNCTION = text(
    "SELECT pg_get_functiondef(oid) FROM pg_proc WHERE proname = 'course_change_violations'"
)


async def test_upgrade_builds_the_function_the_model_declares(
    migration_db_url: str, db_session
) -> None:
    declared = (await db_session.scalars(_FUNCTION)).all()
    assert len(declared) == 1
    _alembic(migration_db_url, "upgrade", REVISION)
    engine = create_async_engine(migration_db_url)
    try:
        async with engine.connect() as conn:
            assert (await conn.scalars(_FUNCTION)).all() == declared

        # A second run, after a rollback that kept the function, changes nothing.
        _alembic(migration_db_url, "stamp", BEFORE)
        _alembic(migration_db_url, "upgrade", REVISION)
        async with engine.connect() as conn:
            assert (await conn.scalars(_FUNCTION)).all() == declared

        _alembic(migration_db_url, "downgrade", BEFORE)
        async with engine.connect() as conn:
            assert (await conn.scalars(_FUNCTION)).all() == []
    finally:
        await engine.dispose()
