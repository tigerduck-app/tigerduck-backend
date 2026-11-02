"""The `device_registrations.secret_hash` migration, run by Alembic on a
database of its own the way a deploy runs it (`entrypoint.sh`)."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from server.tests.conftest import _alembic

pytestmark = pytest.mark.asyncio(loop_scope="session")

REVISION = "9aa3f2c27919"
DOWN_REVISION = "452fe3a32c93"

_SEED = """
INSERT INTO device_registrations
    (device_id, user_id, platform, pts_token_hex, device_token_hex, bundle_id,
     attrs_type, apns_env)
VALUES ('iphone-1', 'anon-iphone-1', 'apple', '', 'apns-token', 'b', '', '')
"""

_COLUMN = """
SELECT data_type, character_maximum_length, is_nullable
FROM information_schema.columns
WHERE table_name = 'device_registrations' AND column_name = 'secret_hash'
"""


async def test_rows_keep_no_secret_and_the_downgrade_drops_the_column(
    migration_db_url: str,
) -> None:
    _alembic(migration_db_url, "upgrade", DOWN_REVISION)
    engine = create_async_engine(migration_db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(_SEED))

        _alembic(migration_db_url, "upgrade", REVISION)
        async with engine.connect() as conn:
            column = (await conn.execute(text(_COLUMN))).one()
            assert tuple(column) == ("character varying", 64, "YES")
            hashes = (
                await conn.execute(text("SELECT secret_hash FROM device_registrations"))
            ).scalars().all()
            assert hashes == [None]

        # A rollback stamps the database back and keeps the column; the next
        # start upgrades over it again.
        _alembic(migration_db_url, "stamp", DOWN_REVISION)
        _alembic(migration_db_url, "upgrade", REVISION)
        _alembic(migration_db_url, "downgrade", DOWN_REVISION)
        async with engine.connect() as conn:
            assert (await conn.execute(text(_COLUMN))).first() is None
            assert (
                await conn.execute(text("SELECT count(*) FROM device_registrations"))
            ).scalar_one() == 1
    finally:
        await engine.dispose()
