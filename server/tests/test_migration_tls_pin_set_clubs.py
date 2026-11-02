"""The migration that seeds the `clubs` pin set, run by Alembic on a database
of its own the way a deploy runs it, and the documents signed after it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from server.db import build_session_factory, session_scope
from server.tests.conftest import _alembic
from server.tls_pins.document import (
    PinSigner,
    document_sets,
    ensure_current_document,
    publish,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

REVISION = "9e90a76a367e"
DOWN_REVISION = "bdeb139d9662"
NOW = datetime(2026, 11, 3, tzinfo=UTC)

CLUBS_ENTRY = {
    "id": "clubs",
    "domains": [{"name": "clubs.ntust.edu.tw", "include_subdomains": False}],
    "pins": [
        "C5+lpZ7tcVwmwQIMcRtPbsQtWLABXhQzejna0wHFr8M=",
        "diGVwiVYbubAI3RW4hB9xU8e/CH2GnkuvVFZE8zmgzI=",
        "fk6IOKit1ild5647BH06ujSIq5XbCgqlbYl6ANhhi88=",
        "sCkq5UWXjg+7mKu9lMhhYF5bGLsy7VI/UNW3tccdR7w=",
    ],
    "expires_at": "2027-07-21T00:00:00Z",
}

_CLUBS = text(
    "SELECT domains, pins, expires_at, monitored_hosts, auto_publish, updated_by"
    " FROM tls_pin_sets WHERE id = 'clubs'"
)
_SET_IDS = text("SELECT id FROM tls_pin_sets ORDER BY id")


async def test_the_set_is_seeded_and_signed_with_the_next_publish(
    migration_db_url: str,
) -> None:
    signer = PinSigner(key_id="td-pins-test", key=Ed25519PrivateKey.generate())
    _alembic(migration_db_url, "upgrade", DOWN_REVISION)
    engine = create_async_engine(migration_db_url)
    factory = build_session_factory(engine)
    try:
        # The document served before the deploy.
        async with session_scope(factory) as session:
            await ensure_current_document(session, signer, now=NOW)

        _alembic(migration_db_url, "upgrade", REVISION)
        async with engine.connect() as conn:
            clubs = (await conn.execute(_CLUBS)).one()
        assert clubs.domains == CLUBS_ENTRY["domains"]
        assert [p["spki"] for p in clubs.pins] == CLUBS_ENTRY["pins"]
        assert {p["source"] for p in clubs.pins} == {"builtin"}
        assert clubs.expires_at == datetime(2027, 7, 21, tzinfo=UTC)
        assert clubs.monitored_hosts == ["clubs.ntust.edu.tw"]
        assert not clubs.auto_publish
        assert clubs.updated_by == "migration"

        # A re-sign keeps the served document's sets; a publish signs the new one.
        async with session_scope(factory) as session:
            resigned = await ensure_current_document(
                session, signer, now=NOW + timedelta(days=23)
            )
            assert [s["id"] for s in document_sets(resigned)] == ["library", "ntust"]
            published = await publish(
                session, signer, reason="portal.publish", now=NOW + timedelta(days=24)
            )
        signer.key.public_key().verify(published.signature, published.payload)
        sets = {s["id"]: s for s in document_sets(published)}
        assert sorted(sets) == ["clubs", "library", "ntust"]
        assert sets["clubs"] == CLUBS_ENTRY

        # A second run, after a rollback by stamp, keeps what an operator changed.
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE tls_pin_sets SET auto_publish = true WHERE id = 'clubs'")
            )
        _alembic(migration_db_url, "stamp", DOWN_REVISION)
        _alembic(migration_db_url, "upgrade", REVISION)
        async with engine.connect() as conn:
            assert (await conn.execute(_CLUBS)).one().auto_publish

        _alembic(migration_db_url, "downgrade", DOWN_REVISION)
        async with engine.connect() as conn:
            assert (await conn.scalars(_SET_IDS)).all() == ["library", "ntust"]
    finally:
        await engine.dispose()
