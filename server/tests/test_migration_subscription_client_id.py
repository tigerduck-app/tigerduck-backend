"""The subscription `client_id` migration, run by Alembic on a database of
its own the way a deploy runs it (`entrypoint.sh`)."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from server.tests.conftest import _alembic

pytestmark = pytest.mark.asyncio(loop_scope="session")

REVISION = "452fe3a32c93"
DOWN_REVISION = "9f599ff4ebb6"

_SEED = """
WITH u AS (INSERT INTO users DEFAULT VALUES RETURNING id),
     d AS (
         INSERT INTO user_devices (user_id, client_device_id, platform)
         SELECT id, 'iphone', 'ios' FROM u RETURNING id, user_id
     )
INSERT INTO user_bulletin_subscriptions (user_id, device_id, name)
SELECT user_id, id, rule FROM d, (VALUES ('a'), ('b')) AS rules (rule)
"""


async def test_every_rule_gets_its_own_client_id_and_a_live_one_is_unique(
    migration_db_url: str,
) -> None:
    _alembic(migration_db_url, "upgrade", DOWN_REVISION)
    engine = create_async_engine(migration_db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(_SEED))

        _alembic(migration_db_url, "upgrade", REVISION)
        async with engine.begin() as conn:
            ids = (
                await conn.execute(text("SELECT client_id FROM user_bulletin_subscriptions"))
            ).scalars().all()
            assert len(ids) == 2 and None not in ids and len(set(ids)) == 2
            # An insert naming none gets one, as the /v3 PUT's do.
            await conn.execute(
                text(
                    "INSERT INTO user_bulletin_subscriptions (user_id, device_id, name)"
                    " SELECT user_id, device_id, 'c' FROM user_bulletin_subscriptions LIMIT 1"
                )
            )
        # A deleted rule frees its id; a live one keeps it.
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE user_bulletin_subscriptions SET deleted_at = now()"
                    " WHERE name = 'a'"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO user_bulletin_subscriptions (user_id, device_id, client_id)"
                    " SELECT user_id, device_id, client_id FROM user_bulletin_subscriptions"
                    " WHERE name = 'a'"
                )
            )
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO user_bulletin_subscriptions (user_id, device_id, client_id)"
                        " SELECT user_id, device_id, client_id FROM user_bulletin_subscriptions"
                        " WHERE name = 'b'"
                    )
                )

        # A rollback stamps the database back and keeps the column
        # (MIGRATE.md); the next start upgrades over it again.
        _alembic(migration_db_url, "stamp", DOWN_REVISION)
        _alembic(migration_db_url, "upgrade", REVISION)
        _alembic(migration_db_url, "downgrade", DOWN_REVISION)
        async with engine.connect() as conn:
            columns = (
                await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns"
                        " WHERE table_name = 'user_bulletin_subscriptions'"
                    )
                )
            ).scalars().all()
            assert "client_id" not in columns
    finally:
        await engine.dispose()
