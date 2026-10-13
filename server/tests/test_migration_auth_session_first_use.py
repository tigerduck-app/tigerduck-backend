"""The auth-session migration that adds `first_used_at`, `rerotated_at` and
the revoked reason `superseded`, run by Alembic on a database of its own
the way a deploy runs it."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from server.tests.conftest import _alembic

pytestmark = pytest.mark.asyncio(loop_scope="session")

REVISION = "cc54925b7852"
DOWN_REVISION = "ddf188f91a15"
NEW_COLUMNS = {"first_used_at", "rerotated_at"}

# A rotated token whose grace retry the previous release marked in
# reuse_detected_at, and the successor that retry made.
_GRACE_RETRIED_LINEAGE = """
WITH u AS (INSERT INTO users DEFAULT VALUES RETURNING id),
successor AS (
    INSERT INTO auth_sessions (user_id, refresh_token_hash, expires_at)
    SELECT id, 'successor', now() + interval '90 days' FROM u
    RETURNING id, user_id
)
INSERT INTO auth_sessions
    (user_id, refresh_token_hash, expires_at, revoked_at, revoked_reason,
     replaced_by_session_id, reuse_detected_at)
SELECT user_id, 'retried', now() + interval '90 days', now(), 'rotated', id, now()
FROM successor
"""


async def _session_columns(conn: AsyncConnection) -> set[str]:
    rows = await conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name = 'auth_sessions'"
        )
    )
    return set(rows.scalars())


async def _reason_checks(conn: AsyncConnection) -> list[str]:
    rows = await conn.execute(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
            " WHERE conname = 'chk_revoked_reason'"
        )
    )
    return list(rows.scalars())


async def test_upgrade_adds_the_columns_and_downgrade_restores_the_schema(
    migration_db_url: str,
) -> None:
    _alembic(migration_db_url, "upgrade", DOWN_REVISION)
    engine = create_async_engine(migration_db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(_GRACE_RETRIED_LINEAGE))

        _alembic(migration_db_url, "upgrade", REVISION)
        async with engine.begin() as conn:
            assert NEW_COLUMNS <= await _session_columns(conn)
            row = (
                await conn.execute(
                    text(
                        "SELECT revoked_reason, reuse_detected_at IS NOT NULL,"
                        " first_used_at, rerotated_at"
                        " FROM auth_sessions WHERE refresh_token_hash = 'retried'"
                    )
                )
            ).one()
            # Existing rows stay as they are.
            assert tuple(row) == ("rotated", True, None, None)
            await conn.execute(
                text(
                    "UPDATE auth_sessions SET revoked_at = now(),"
                    " revoked_reason = 'superseded',"
                    " replaced_by_session_id = ("
                    "     SELECT id FROM auth_sessions"
                    "     WHERE refresh_token_hash = 'retried')"
                    " WHERE refresh_token_hash = 'successor'"
                )
            )

        _alembic(migration_db_url, "downgrade", DOWN_REVISION)
        async with engine.begin() as conn:
            assert not NEW_COLUMNS & await _session_columns(conn)
            row = (
                await conn.execute(
                    text(
                        "SELECT revoked_reason, replaced_by_session_id"
                        " FROM auth_sessions WHERE refresh_token_hash = 'successor'"
                    )
                )
            ).one()
            # How the previous release records a successor its grace retry
            # replaced.
            assert tuple(row) == ("rotated", None)
        with pytest.raises(IntegrityError, match="chk_revoked_reason"):
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE auth_sessions SET revoked_reason = 'superseded'"
                        " WHERE refresh_token_hash = 'successor'"
                    )
                )
    finally:
        await engine.dispose()


async def test_upgrade_after_a_rollback_by_stamp_keeps_what_was_recorded(
    migration_db_url: str,
) -> None:
    """Rolling back to the previous release stamps the database back and
    keeps the columns (MIGRATE.md); the next start of this release upgrades
    over them again."""
    _alembic(migration_db_url, "upgrade", REVISION)
    engine = create_async_engine(migration_db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(_GRACE_RETRIED_LINEAGE))
            await conn.execute(
                text(
                    "UPDATE auth_sessions SET first_used_at = now(),"
                    " rerotated_at = now() WHERE refresh_token_hash = 'retried'"
                )
            )

        _alembic(migration_db_url, "stamp", DOWN_REVISION)
        _alembic(migration_db_url, "upgrade", REVISION)
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT first_used_at IS NOT NULL, rerotated_at IS NOT NULL"
                        " FROM auth_sessions WHERE refresh_token_hash = 'retried'"
                    )
                )
            ).one()
            assert tuple(row) == (True, True)
            [check] = await _reason_checks(conn)
            assert "superseded" in check

        # The downgrade, too, runs over a schema it already changed.
        _alembic(migration_db_url, "downgrade", DOWN_REVISION)
        _alembic(migration_db_url, "stamp", REVISION)
        _alembic(migration_db_url, "downgrade", DOWN_REVISION)
        async with engine.connect() as conn:
            assert not NEW_COLUMNS & await _session_columns(conn)
            [check] = await _reason_checks(conn)
            assert "superseded" not in check
    finally:
        await engine.dispose()
