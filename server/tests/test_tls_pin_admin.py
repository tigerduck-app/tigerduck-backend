"""The portal's pin actions on the admin listener: each one changes the
sets, publishes when it should, and records the operator."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select

from server.admin_audit import AdminAuditLog
from server.tls_pins.document import PinSigner, document_sets
from server.tls_pins.models import TlsPinDocument, TlsPinSet

pytestmark = pytest.mark.asyncio(loop_scope="session")

ACTOR = {"X-Portal-Actor": "ops@example.com"}
PIN_A = "9VZ7Yd685RTXsE6rL/puuMbnejYaXwaZasGL7c+Uolc="
PIN_B = "BxOmElsPeD54MBOb5h+TPNJnBlsAk6spRc/wLnko0cc="
NEW_PIN = "xES1tmzl1x4bXkDyc4XJXL/SSgW1b3DKwJkvD1DDN5w="


@pytest_asyncio.fixture(loop_scope="session")
async def stored(db_session, client):
    db_session.add(
        TlsPinSet(
            id="ntust",
            domains=[{"name": "ntust.edu.tw", "include_subdomains": True}],
            pins=[
                {"spki": p, "label": "CA", "first_seen_at": None, "last_seen_at": None,
                 "source": "builtin"}
                for p in (PIN_A, PIN_B)
            ],
            expires_at=datetime(2027, 8, 18, tzinfo=UTC),
            monitored_hosts=["www.ntust.edu.tw"],
            watch_results={"www.ntust.edu.tw": {"status": "trusted"}, "old.ntust.edu.tw": {}},
        )
    )
    await db_session.commit()
    client.app.state.tls_pin_signer = PinSigner("td-pins-test", Ed25519PrivateKey.generate())
    return db_session


async def _set(db_session) -> TlsPinSet:
    db_session.expire_all()
    return await db_session.get(TlsPinSet, "ntust")


async def _latest(db_session) -> TlsPinDocument | None:
    return (
        await db_session.execute(
            select(TlsPinDocument).order_by(TlsPinDocument.serial.desc()).limit(1)
        )
    ).scalar_one_or_none()


async def _audit(db_session) -> list[tuple[str, str, dict]]:
    rows = await db_session.execute(
        select(AdminAuditLog.actor, AdminAuditLog.action, AdminAuditLog.detail).order_by(
            AdminAuditLog.id
        )
    )
    return [tuple(r) for r in rows]


async def test_adding_a_pin_publishes_it_and_records_the_operator(admin_client, stored) -> None:
    resp = await admin_client.post(
        "/tls-pins/sets/ntust/pins", json={"spki": NEW_PIN, "label": "TWCA Global Root CA"},
        headers=ACTOR,
    )

    assert resp.status_code == 200
    serial = resp.json()["serial"]
    pins = (await _set(stored)).pins
    assert [p["spki"] for p in pins] == [PIN_A, PIN_B, NEW_PIN]
    assert pins[-1]["source"] == "portal"
    latest = await _latest(stored)
    assert latest.serial == serial and latest.reason == "portal.add_pin"
    assert document_sets(latest)[0]["pins"] == [PIN_A, PIN_B, NEW_PIN]
    assert await _audit(stored) == [
        ("ops@example.com", "tls_pins.add_pin", {"set": "ntust", "spki": NEW_PIN, "serial": serial})
    ]


async def test_a_pin_must_be_a_new_sha256_in_padded_base64(admin_client, stored) -> None:
    for spki, code in ((PIN_A, 409), ("not-base64", 422), (NEW_PIN.rstrip("="), 422)):
        resp = await admin_client.post(
            "/tls-pins/sets/ntust/pins", json={"spki": spki, "label": "x"}, headers=ACTOR
        )
        assert resp.status_code == code, spki
    assert await _latest(stored) is None


async def test_an_action_needs_an_operator(admin_client, stored) -> None:
    resp = await admin_client.post("/tls-pins/publish")

    assert resp.status_code == 422
    assert await _latest(stored) is None


async def test_removing_a_pin_publishes_but_the_last_one_stays(admin_client, stored) -> None:
    removed = await admin_client.post(
        "/tls-pins/sets/ntust/pins/remove", json={"spki": PIN_A}, headers=ACTOR
    )
    last = await admin_client.post(
        "/tls-pins/sets/ntust/pins/remove", json={"spki": PIN_B}, headers=ACTOR
    )
    unknown = await admin_client.post(
        "/tls-pins/sets/ntust/pins/remove", json={"spki": NEW_PIN}, headers=ACTOR
    )

    assert removed.status_code == 200 and removed.json()["serial"] is not None
    assert last.status_code == 409 and unknown.status_code == 404
    assert [p["spki"] for p in (await _set(stored)).pins] == [PIN_B]
    assert document_sets(await _latest(stored))[0]["pins"] == [PIN_B]


async def test_monitored_hosts_are_saved_without_publishing(admin_client, stored) -> None:
    resp = await admin_client.put(
        "/tls-pins/sets/ntust/monitored-hosts",
        json={"hosts": ["www.ntust.edu.tw", "mail.ntust.edu.tw:993", "www.ntust.edu.tw"]},
        headers=ACTOR,
    )
    bad = await admin_client.put(
        "/tls-pins/sets/ntust/monitored-hosts",
        json={"hosts": ["https://www.ntust.edu.tw/"]},
        headers=ACTOR,
    )

    assert resp.status_code == 200 and resp.json()["serial"] is None
    assert bad.status_code == 422
    pin_set = await _set(stored)
    assert pin_set.monitored_hosts == ["www.ntust.edu.tw", "mail.ntust.edu.tw:993"]
    assert list(pin_set.watch_results) == ["www.ntust.edu.tw"]
    assert await _latest(stored) is None


async def test_an_expiry_can_only_move_later(admin_client, stored) -> None:
    earlier = await admin_client.put(
        "/tls-pins/sets/ntust/expires-at", json={"expires_at": "2027-01-01T00:00:00Z"},
        headers=ACTOR,
    )
    later = await admin_client.put(
        "/tls-pins/sets/ntust/expires-at", json={"expires_at": "2028-08-18T00:00:00Z"},
        headers=ACTOR,
    )

    assert earlier.status_code == 409 and later.status_code == 200
    assert document_sets(await _latest(stored))[0]["expires_at"] == "2028-08-18T00:00:00Z"


async def test_auto_publish_is_toggled_and_recorded(admin_client, stored) -> None:
    resp = await admin_client.put(
        "/tls-pins/sets/ntust/auto-publish", json={"enabled": True}, headers=ACTOR
    )

    assert resp.status_code == 200
    assert (await _set(stored)).auto_publish is True
    assert (await _audit(stored))[-1][1:] == (
        "tls_pins.set_auto_publish", {"set": "ntust", "enabled": True, "serial": None}
    )


async def test_publish_and_resign_now(admin_client, stored) -> None:
    published = await admin_client.post("/tls-pins/publish", headers=ACTOR)
    await admin_client.post(
        "/tls-pins/sets/ntust/pins", json={"spki": NEW_PIN, "label": "x"}, headers=ACTOR
    )
    stored.add(
        TlsPinSet(
            id="library", domains=[], pins=[], expires_at=datetime(2027, 5, 4, tzinfo=UTC)
        )
    )
    await stored.commit()
    resigned = await admin_client.post("/tls-pins/resign", headers=ACTOR)

    assert published.status_code == 200 and resigned.status_code == 200
    latest = await _latest(stored)
    assert latest.serial == resigned.json()["serial"] > published.json()["serial"]
    # The re-sign keeps the served sets, not the stored ones.
    assert [s["id"] for s in document_sets(latest)] == ["ntust"]
    assert json.loads(latest.payload)["pin_sets"][0]["pins"] == [PIN_A, PIN_B, NEW_PIN]


async def test_without_a_key_edits_are_kept_and_publishing_is_refused(
    admin_client, stored
) -> None:
    admin_client.app.state.tls_pin_signer = None

    edit = await admin_client.post(
        "/tls-pins/sets/ntust/pins", json={"spki": NEW_PIN, "label": "x"}, headers=ACTOR
    )
    publish = await admin_client.post("/tls-pins/publish", headers=ACTOR)

    assert edit.status_code == 200 and edit.json()["serial"] is None
    assert publish.status_code == 503
    assert len((await _set(stored)).pins) == 3
    assert await _latest(stored) is None


async def test_the_actions_are_not_served_on_the_api_port(client, stored) -> None:
    resp = await client.post("/tls-pins/publish", headers=ACTOR)

    assert resp.status_code == 404
