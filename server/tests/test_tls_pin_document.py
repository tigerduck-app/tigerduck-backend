"""The signed pin document: signing, serials, re-signing and the endpoint
the apps fetch it from."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)
from prometheus_client import REGISTRY
from sqlalchemy import select

from server.db import build_session_factory, session_scope
from server.tls_pins.document import (
    PinSigner,
    document_sets,
    ensure_current_document,
    prompt_roots,
    publish,
)
from server.tls_pins.models import TlsPinDocument, TlsPinSet

pytestmark = pytest.mark.asyncio(loop_scope="session")

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 25, 4, 0, tzinfo=UTC)
PIN = "9VZ7Yd685RTXsE6rL/puuMbnejYaXwaZasGL7c+Uolc="


def _published(set_id: str) -> float:
    return REGISTRY.get_sample_value("tigerduck_tls_pins_published_total", {"set": set_id}) or 0.0


def _unb64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def make_set(set_id: str = "ntust", *, pins: tuple[str, ...] = (PIN,), **fields) -> TlsPinSet:
    return TlsPinSet(
        id=set_id,
        domains=[{"name": f"{set_id}.example", "include_subdomains": True}],
        pins=[
            {"spki": p, "label": "CA", "first_seen_at": None, "last_seen_at": None, "source": "builtin"}
            for p in pins
        ],
        expires_at=datetime(2027, 8, 18, tzinfo=UTC),
        monitored_hosts=[],
        **fields,
    )


@pytest.fixture
def signer() -> PinSigner:
    return PinSigner(key_id="td-pins-test", key=Ed25519PrivateKey.generate())


@pytest_asyncio.fixture(loop_scope="session")
async def stored_set(db_session):
    db_session.add(make_set())
    await db_session.commit()


async def test_a_published_document_round_trips_through_the_endpoint(
    client, db_session, stored_set, signer
) -> None:
    document = await publish(db_session, signer, reason="initial", now=NOW)
    await db_session.commit()

    resp = await client.get("/.well-known/tigerduck/tls-pins.json")

    assert resp.status_code == 200
    assert resp.content == document.envelope
    assert resp.headers["content-type"] == "application/json"
    assert resp.headers["cache-control"] == "public, max-age=300"
    assert resp.headers["etag"] == f'"{document.serial}"'
    envelope = json.loads(resp.content)
    assert envelope["format"] == 1 and envelope["key_id"] == "td-pins-test"
    payload_bytes = _unb64url(envelope["payload"])
    signer.key.public_key().verify(_unb64url(envelope["signature"]), payload_bytes)
    payload = json.loads(payload_bytes)
    assert payload["serial"] == document.serial == int(NOW.timestamp())
    assert payload["issued_at"] == "2026-10-25T04:00:00Z"
    assert payload["expires_at"] == "2026-11-24T04:00:00Z"
    assert payload["pin_sets"] == [
        {
            "id": "ntust",
            "domains": [{"name": "ntust.example", "include_subdomains": True}],
            "pins": [PIN],
            "expires_at": "2027-08-18T00:00:00Z",
        }
    ]
    assert payload["prompt_roots"] == list(prompt_roots())


async def test_a_tampered_payload_fails_verification(db_session, stored_set, signer) -> None:
    document = await publish(db_session, signer, reason="initial", now=NOW)
    envelope = json.loads(document.envelope)
    tampered = _unb64url(envelope["payload"]).replace(PIN.encode(), b"A" * len(PIN))

    with pytest.raises(InvalidSignature):
        signer.key.public_key().verify(_unb64url(envelope["signature"]), tampered)


async def test_the_endpoint_needs_no_auth_and_answers_404_without_a_document(client) -> None:
    resp = await client.get("/.well-known/tigerduck/tls-pins.json")

    assert resp.status_code == 404
    assert resp.json() == {"detail": "tls_pin_document_not_found"}


async def test_the_highest_serial_is_served(client, db_session, stored_set, signer) -> None:
    await publish(db_session, signer, reason="initial", now=NOW)
    newer = await publish(db_session, signer, reason="portal", now=NOW)
    await db_session.commit()

    resp = await client.get("/.well-known/tigerduck/tls-pins.json")

    assert newer.serial == int(NOW.timestamp()) + 1
    assert resp.content == newer.envelope


async def test_a_restored_database_never_lowers_the_serial(db_session, stored_set, signer) -> None:
    first = await publish(db_session, signer, reason="initial", now=NOW - timedelta(days=20))
    lost = await publish(db_session, signer, reason="portal", now=NOW - timedelta(days=1))
    await db_session.delete(lost)
    await db_session.flush()

    after_restore = await publish(db_session, signer, reason="portal", now=NOW)

    assert after_restore.serial > lost.serial > first.serial


async def test_concurrent_publishes_get_distinct_increasing_serials(
    prepared_engine, db_session, stored_set, signer
) -> None:
    factory = build_session_factory(prepared_engine)

    async def one() -> int:
        async with session_scope(factory) as session:
            document = await publish(session, signer, reason="portal", now=NOW)
            # Holds the lock while the others wait on it.
            await asyncio.sleep(0.05)
            return document.serial

    serials = await asyncio.gather(*(one() for _ in range(5)))

    assert sorted(serials) == list(range(int(NOW.timestamp()), int(NOW.timestamp()) + 5))


async def test_the_first_document_is_published_once(db_session, stored_set, signer) -> None:
    first = await ensure_current_document(db_session, signer, now=NOW)
    again = await ensure_current_document(db_session, signer, now=NOW + timedelta(hours=1))

    assert first is not None and first.reason == "initial"
    assert again is None


async def test_nothing_is_published_without_a_key(db_session, stored_set) -> None:
    assert await ensure_current_document(db_session, None, now=NOW) is None
    assert (await db_session.execute(select(TlsPinDocument))).first() is None


async def test_a_document_is_resigned_with_its_own_sets_seven_days_before_expiry(
    db_session, stored_set, signer
) -> None:
    first = await ensure_current_document(db_session, signer, now=NOW)
    # An unpublished change stays unpublished through a re-sign.
    pin_set = await db_session.get(TlsPinSet, "ntust")
    pin_set.pins = [*pin_set.pins, {**pin_set.pins[0], "spki": "B" * 43 + "="}]
    await db_session.flush()
    published = _published("ntust")

    early = await ensure_current_document(db_session, signer, now=NOW + timedelta(days=22, hours=23))
    resigned = await ensure_current_document(db_session, signer, now=NOW + timedelta(days=23))

    assert early is None
    assert resigned is not None and resigned.reason == "resign"
    assert resigned.serial > first.serial
    assert document_sets(resigned) == document_sets(first)
    assert resigned.expires_at == NOW + timedelta(days=53)
    assert _published("ntust") == published


async def test_a_changed_set_counts_as_published(db_session, stored_set, signer) -> None:
    await publish(db_session, signer, reason="initial", now=NOW)
    before = _published("ntust")
    pin_set = await db_session.get(TlsPinSet, "ntust")
    pin_set.expires_at = datetime(2028, 1, 1, tzinfo=UTC)
    await db_session.flush()

    await publish(db_session, signer, reason="portal", now=NOW)

    assert _published("ntust") == before + 1
