"""The migration that adds `links` and seeds the academic calendar link,
run by Alembic on a database of its own the way a deploy runs it."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from server.tests.conftest import _alembic

pytestmark = pytest.mark.asyncio(loop_scope="session")

REVISION = "35df7613737a"
BEFORE = "c5e8a2f71d40"


async def test_upgrade_seeds_the_calendar_and_keeps_an_edited_one(
    migration_db_url: str,
) -> None:
    _alembic(migration_db_url, "upgrade", REVISION)
    engine = create_async_engine(migration_db_url)
    try:
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT target_url, enabled, expectations,"
                        " check_interval_minutes, alert_state FROM links"
                        " WHERE name = 'ntust-calendar'"
                    )
                )
            ).one()
            assert row == (
                "https://www.academic.ntust.edu.tw/p/404-1048-78935.php",
                True,
                [
                    {"type": "status", "equals": 200},
                    {"type": "final_host", "in": ["www.academic.ntust.edu.tw"]},
                    {"type": "href_suffix_count", "suffix": ".ics", "min": 1},
                ],
                60,
                "ok",
            )
            await conn.execute(
                text("UPDATE links SET target_url = 'https://www.ntust.edu.tw/'")
            )

        # A rollback stamps the database back; the next start upgrades over it.
        _alembic(migration_db_url, "stamp", BEFORE)
        _alembic(migration_db_url, "upgrade", REVISION)
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT target_url FROM links")) == (
                "https://www.ntust.edu.tw/"
            )

        _alembic(migration_db_url, "downgrade", BEFORE)
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT to_regclass('links')")) is None
    finally:
        await engine.dispose()
