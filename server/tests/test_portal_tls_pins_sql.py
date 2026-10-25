"""The portal's TLS pins page (`portal/app/routes/tls_pins.py`) read against
the tables the backend migrates; the portal's own tests have no database."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from server.tls_pins.document import PinSigner, publish, timestamp
from server.tls_pins.models import TlsPinSet

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from portal.app.db import _asyncpg_dsn
from portal.app.routes.tls_pins import get_tls_pins

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(loop_scope="session")
async def pool(db_session, test_settings):
    p = await asyncpg.create_pool(_asyncpg_dsn(test_settings.database_url), min_size=1, max_size=2)
    try:
        yield p
    finally:
        await p.close()


async def test_the_page_lists_sets_stale_pins_results_and_documents(db_session, pool) -> None:
    now = datetime.now(UTC)
    db_session.add(
        TlsPinSet(
            id="ntust",
            domains=[{"name": "ntust.edu.tw", "include_subdomains": True}],
            pins=[
                {"spki": "fresh", "label": "a", "first_seen_at": timestamp(now),
                 "last_seen_at": timestamp(now), "source": "builtin"},
                {"spki": "old", "label": "b", "first_seen_at": None,
                 "last_seen_at": timestamp(now - timedelta(days=91)), "source": "watcher",
                 "pending": True},
                {"spki": "never", "label": "c", "first_seen_at": None,
                 "last_seen_at": None, "source": "builtin"},
            ],
            expires_at=datetime(2027, 8, 18, tzinfo=UTC),
            monitored_hosts=["www.ntust.edu.tw"],
            watch_results={"www.ntust.edu.tw": {"status": "trusted"}},
        )
    )
    await db_session.flush()
    await publish(db_session, PinSigner("td-pins-test", Ed25519PrivateKey.generate()), reason="initial")
    await db_session.commit()

    body = json.loads((await get_tls_pins(pool=pool)).body)

    (pin_set,) = body["sets"]
    assert [p["stale"] for p in pin_set["pins"]] == [False, True, False]
    assert [p.get("pending", False) for p in pin_set["pins"]] == [False, True, False]
    assert pin_set["watch_results"] == {"www.ntust.edu.tw": {"status": "trusted"}}
    assert pin_set["monitored_hosts"] == ["www.ntust.edu.tw"]
    assert [d["reason"] for d in body["documents"]] == ["initial"]
    assert "envelope" not in body["documents"][0]
