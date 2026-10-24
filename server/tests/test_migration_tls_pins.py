"""The migration that adds the pin tables and seeds the apps' built-in sets,
run by Alembic on a database of its own the way a deploy runs it."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from server.tests.conftest import _alembic

pytestmark = pytest.mark.asyncio(loop_scope="session")

REVISION = "8996a3bf992f"
BEFORE = "c5e8a2f71d40"


async def test_upgrade_seeds_both_sets_with_auto_publish_off(migration_db_url: str) -> None:
    _alembic(migration_db_url, "upgrade", REVISION)
    engine = create_async_engine(migration_db_url)
    try:
        async with engine.begin() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT id, domains, pins, expires_at, monitored_hosts, auto_publish"
                        " FROM tls_pin_sets ORDER BY id"
                    )
                )
            ).all()
            assert [r.id for r in rows] == ["library", "ntust"]
            library, ntust = rows
            assert not library.auto_publish and not ntust.auto_publish
            assert ntust.domains == [{"name": "ntust.edu.tw", "include_subdomains": True}]
            assert [p["spki"] for p in ntust.pins] == [
                "9VZ7Yd685RTXsE6rL/puuMbnejYaXwaZasGL7c+Uolc=",
                "BxOmElsPeD54MBOb5h+TPNJnBlsAk6spRc/wLnko0cc=",
                "xES1tmzl1x4bXkDyc4XJXL/SSgW1b3DKwJkvD1DDN5w=",
                "BmALlMAxi7aWfwx3h8yKEDKhecTpXjxXYLMuKQ9/7Js=",
            ]
            assert {p["source"] for p in ntust.pins + library.pins} == {"builtin"}
            assert len(library.pins) == 3
            assert ntust.expires_at.isoformat() == "2027-08-18T00:00:00+00:00"
            assert library.expires_at.isoformat() == "2027-05-04T00:00:00+00:00"
            assert "mail.ntust.edu.tw:993" in ntust.monitored_hosts
            assert len(ntust.monitored_hosts) == 10
            assert library.monitored_hosts == ["api.lib.ntust.edu.tw"]
            await conn.execute(text("UPDATE tls_pin_sets SET auto_publish = true"))

        # A second run over the same schema keeps what an operator changed.
        _alembic(migration_db_url, "stamp", BEFORE)
        _alembic(migration_db_url, "upgrade", REVISION)
        async with engine.connect() as conn:
            assert await conn.scalar(
                text("SELECT bool_and(auto_publish) FROM tls_pin_sets")
            )

        _alembic(migration_db_url, "downgrade", BEFORE)
        async with engine.connect() as conn:
            assert not await conn.scalar(
                text("SELECT count(*) FROM pg_tables WHERE tablename LIKE 'tls_pin_%'")
            )
        _alembic(migration_db_url, "stamp", REVISION)
        _alembic(migration_db_url, "downgrade", BEFORE)
    finally:
        await engine.dispose()
