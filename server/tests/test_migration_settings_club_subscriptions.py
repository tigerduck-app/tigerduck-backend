"""The migration that admits the `club_subscriptions` settings namespace, run
by Alembic on a database of its own the way a deploy runs it."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from server.tests.conftest import _alembic

pytestmark = pytest.mark.asyncio(loop_scope="session")

REVISION = "bdeb139d9662"
DOWN_REVISION = "9aa3f2c27919"

_CHECK = text(
    "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
    " WHERE conname = 'chk_settings_namespace'"
)
_INSERT = text(
    "INSERT INTO user_settings_documents (user_id, namespace, document)"
    " VALUES (:user_id, :namespace, '{}')"
)
_NAMESPACES = text("SELECT namespace FROM user_settings_documents ORDER BY namespace")


async def test_upgrade_admits_the_namespace_and_the_downgrade_drops_its_documents(
    migration_db_url: str, db_session
) -> None:
    declared = (await db_session.scalars(_CHECK)).one()
    _alembic(migration_db_url, "upgrade", DOWN_REVISION)
    engine = create_async_engine(migration_db_url)
    try:
        async with engine.begin() as conn:
            user_id = await conn.scalar(
                text("INSERT INTO users DEFAULT VALUES RETURNING id")
            )
            await conn.execute(_INSERT, {"user_id": user_id, "namespace": "notification"})
        with pytest.raises(IntegrityError, match="chk_settings_namespace"):
            async with engine.begin() as conn:
                await conn.execute(
                    _INSERT, {"user_id": user_id, "namespace": "club_subscriptions"}
                )

        _alembic(migration_db_url, "upgrade", REVISION)
        async with engine.begin() as conn:
            assert (await conn.scalars(_CHECK)).one() == declared
            await conn.execute(
                _INSERT, {"user_id": user_id, "namespace": "club_subscriptions"}
            )

        # A rollback by stamp keeps the documents; the next start runs the
        # upgrade over them again.
        _alembic(migration_db_url, "stamp", DOWN_REVISION)
        _alembic(migration_db_url, "upgrade", REVISION)
        async with engine.connect() as conn:
            assert (await conn.scalars(_NAMESPACES)).all() == [
                "club_subscriptions",
                "notification",
            ]

        _alembic(migration_db_url, "downgrade", DOWN_REVISION)
        async with engine.connect() as conn:
            assert (await conn.scalars(_NAMESPACES)).all() == ["notification"]
            assert "club_subscriptions" not in (await conn.scalars(_CHECK)).one()
    finally:
        await engine.dispose()
