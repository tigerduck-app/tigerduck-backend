"""The admin_audit_log table the portal writes, its migration and its pruning."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from server.admin_audit import AdminAuditLog, prune_admin_audit_log
from server.config import Settings
from server.db import build_session_factory
from server.tests.conftest import _alembic

pytestmark = pytest.mark.asyncio(loop_scope="session")

REVISION = "d07bc39dde6c"
BEFORE = "516f9bad27f0"

_SCHEMA = """
SELECT
    (SELECT count(*) FROM information_schema.tables
      WHERE table_name = 'admin_audit_log'),
    (SELECT count(*) FROM pg_indexes
      WHERE indexname IN ('idx_admin_audit_ts', 'idx_admin_audit_target_user'))
"""


async def test_migration_creates_the_table_and_survives_a_rerun(
    migration_db_url: str,
) -> None:
    _alembic(migration_db_url, "upgrade", REVISION)
    engine = create_async_engine(migration_db_url)
    try:
        async with engine.begin() as conn:
            row = (await conn.execute(text(_SCHEMA))).one()
            assert tuple(row) == (1, 2)
            await conn.execute(
                text("INSERT INTO admin_audit_log (actor, action) VALUES ('a', 'b')")
            )
            defaults = (
                await conn.execute(
                    text("SELECT ts IS NOT NULL, detail, ip FROM admin_audit_log")
                )
            ).one()
            assert tuple(defaults) == (True, {}, None)

        # A rollback that only stamps back keeps the table and its rows.
        _alembic(migration_db_url, "stamp", BEFORE)
        _alembic(migration_db_url, "upgrade", REVISION)
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT count(*) FROM admin_audit_log")) == 1

        _alembic(migration_db_url, "downgrade", BEFORE)
        async with engine.connect() as conn:
            assert tuple((await conn.execute(text(_SCHEMA))).one()) == (0, 0)
    finally:
        await engine.dispose()


async def test_retention_deletes_only_rows_past_the_window(
    db_session, prepared_engine
) -> None:
    now = datetime.now(UTC)
    db_session.add_all(
        [
            AdminAuditLog(actor="ops", action="old", ts=now - timedelta(days=366)),
            AdminAuditLog(actor="ops", action="new", ts=now - timedelta(days=364)),
        ]
    )
    await db_session.commit()

    deleted = await prune_admin_audit_log(
        build_session_factory(prepared_engine), Settings(admin_audit_retention_days=365)
    )

    assert deleted == 1
    left = (await db_session.execute(select(AdminAuditLog.action))).scalars().all()
    assert left == ["new"]
