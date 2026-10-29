"""The migration that cuts the student ID off the names 3.8.0 stored, run by
Alembic on a database of its own the way a deploy runs it."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from server.tests.conftest import _alembic

pytestmark = pytest.mark.asyncio(loop_scope="session")

REVISION = "199cfb4bbb49"
BEFORE = "ea77513de257"

_NAMES = {
    "B11015000": ("B11015000 @ 王小明", "王小明"),
    "B11015001": ("B11015001@Test Student", "Test Student"),
    "B11015002": ("Test Student", "Test Student"),
    "B11015003": ("B11015003 @ ", "B11015003 @ "),
    "B11015004": (None, None),
}


async def test_upgrade_keeps_the_name_after_the_student_id(
    migration_db_url: str,
) -> None:
    _alembic(migration_db_url, "upgrade", BEFORE)
    engine = create_async_engine(migration_db_url)
    try:
        async with engine.begin() as conn:
            for student_id, (stored, _) in _NAMES.items():
                await conn.execute(
                    text("INSERT INTO users (student_id, display_name) VALUES (:s, :n)"),
                    {"s": student_id, "n": stored},
                )

        _alembic(migration_db_url, "upgrade", REVISION)
        async with engine.connect() as conn:
            rows = dict(
                (await conn.execute(text("SELECT student_id, display_name FROM users"))).all()
            )
        assert rows == {student_id: name for student_id, (_, name) in _NAMES.items()}

        _alembic(migration_db_url, "downgrade", BEFORE)
    finally:
        await engine.dispose()
