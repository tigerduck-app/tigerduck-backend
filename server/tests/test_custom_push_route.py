"""HTTP integration for /v2/custom-push."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from server.bulletins.models import Bulletin
from server.models import CustomPushDispatch

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _session_factory(client: AsyncClient) -> async_sessionmaker:
    # Matches the pattern used by test_bulletin_routes.py: the per-test
    # session factory lives on app.state, swapped in by the conftest
    # `client` fixture after the ASGI lifespan boots.
    return client._transport.app.state.session_factory  # type: ignore[attr-defined]


async def _register_payload(**overrides) -> dict:
    # Mirror a real Apple registration: both standard APNs alert token
    # (device_token_hex) and PTS Live-Activity token (pts_token_hex). The
    # custom-push targeting gate requires device_token_hex on Apple.
    base = {
        "user_id": "u1",
        "device_id": "p1",
        "platform": "apple",
        "device_class": "iphone",
        "pts_token_hex": "ab" * 16,
        "device_token_hex": "cd" * 32,
        "bundle_id": "org.ntust.app.TigerDuck",
        "attrs_type": "TigerDuckActivityAttributes",
        "apns_env": "development",
    }
    base.update(overrides)
    return base


async def test_preview_returns_per_class_counts(client: AsyncClient):
    await client.post(
        "/v2/devices/register",
        json=await _register_payload(device_id="p1", device_class="iphone"),
    )
    await client.post(
        "/v2/devices/register",
        json=await _register_payload(device_id="p2", device_class="ipad"),
    )
    await client.post(
        "/v2/devices/register",
        json=await _register_payload(
            device_id="a1",
            platform="android",
            device_class="android",
            attrs_type=None,
            apns_env=None,
        ),
    )
    resp = await client.post(
        "/v2/custom-push/preview",
        json={"target_classes": ["iphone", "ipad", "android"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()["matched"]
    assert body == {"iphone": 1, "ipad": 1, "android": 1, "total": 3}


async def test_send_keeps_record_creates_bulletin(client: AsyncClient):
    await client.post(
        "/v2/devices/register",
        json=await _register_payload(device_id="p1", device_class="iphone"),
    )
    resp = await client.post(
        "/v2/custom-push",
        json={
            "target_classes": ["iphone"],
            "title": "Maintenance window",
            "body": "Servers will reboot at 03:00.",
            "keeps_record": True,
            "force_ring": True,
        },
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["kind"] == "record"
    assert payload["matched"] == 1
    assert payload["queued"] == 1

    factory = _session_factory(client)
    async with factory() as s:
        bulletin = (
            await s.execute(select(Bulletin).where(Bulletin.source == "custom_push"))
        ).scalar_one()
        assert bulletin.canonical_org == "server"
        assert "server_notification" in bulletin.content_tags
        assert bulletin.dispatch_filter_json["target_classes"] == ["iphone"]
        assert bulletin.dispatch_filter_json["force_ring"] is True


async def test_send_pure_creates_one_dispatch_per_device(client: AsyncClient):
    await client.post(
        "/v2/devices/register",
        json=await _register_payload(device_id="p1", device_class="iphone"),
    )
    await client.post(
        "/v2/devices/register",
        json=await _register_payload(device_id="p2", device_class="iphone"),
    )
    resp = await client.post(
        "/v2/custom-push",
        json={
            "target_classes": ["iphone"],
            "title": "Hi",
            "body": "Body",
            "keeps_record": False,
            "force_ring": False,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["kind"] == "popup"
    assert resp.json()["queued"] == 2

    factory = _session_factory(client)
    async with factory() as s:
        rows = (await s.execute(select(CustomPushDispatch))).scalars().all()
        assert len(rows) == 2
        assert {r.device_id for r in rows} == {"p1", "p2"}
        assert len({r.notification_id for r in rows}) == 2
        assert all(r.force_ring is False for r in rows)


async def test_two_record_sends_same_instant_both_queue(client: AsyncClient):
    # external_id is derived from the unique request_id, not the clock, so
    # two record pushes created back-to-back can't collide on the
    # (source, external_id) unique constraint.
    await client.post(
        "/v2/devices/register",
        json=await _register_payload(device_id="p1", device_class="iphone"),
    )
    for _ in range(2):
        resp = await client.post(
            "/v2/custom-push",
            json={
                "target_classes": ["iphone"],
                "title": "dup",
                "body": "x",
                "keeps_record": True,
                "force_ring": True,
            },
        )
        assert resp.status_code == 200, resp.text

    factory = _session_factory(client)
    async with factory() as s:
        bulletins = (
            await s.execute(select(Bulletin).where(Bulletin.source == "custom_push"))
        ).scalars().all()
        assert len(bulletins) == 2
        assert len({b.external_id for b in bulletins}) == 2


async def test_recent_popup_surfaces_target_classes(client: AsyncClient):
    await client.post(
        "/v2/devices/register",
        json=await _register_payload(device_id="p1", device_class="iphone"),
    )
    await client.post(
        "/v2/custom-push",
        json={
            "target_classes": ["iphone"],
            "title": "pop",
            "body": "y",
            "keeps_record": False,
            "force_ring": True,
        },
    )
    resp = await client.get("/v2/custom-push/recent?limit=10")
    assert resp.status_code == 200
    popup = next(i for i in resp.json() if i["kind"] == "popup")
    assert popup["target_classes"] == ["iphone"]


async def test_send_empty_target_classes_rejected(client: AsyncClient):
    resp = await client.post(
        "/v2/custom-push",
        json={
            "target_classes": [],
            "title": "t",
            "body": "b",
            "keeps_record": True,
            "force_ring": True,
        },
    )
    assert resp.status_code == 422


async def test_recent_includes_both_paths(client: AsyncClient):
    await client.post(
        "/v2/devices/register",
        json=await _register_payload(device_id="p1", device_class="iphone"),
    )
    await client.post(
        "/v2/custom-push",
        json={
            "target_classes": ["iphone"],
            "title": "rec",
            "body": "x",
            "keeps_record": True,
            "force_ring": True,
        },
    )
    await client.post(
        "/v2/custom-push",
        json={
            "target_classes": ["iphone"],
            "title": "pop",
            "body": "y",
            "keeps_record": False,
            "force_ring": True,
        },
    )
    resp = await client.get("/v2/custom-push/recent?limit=10")
    assert resp.status_code == 200
    items = resp.json()
    kinds = {i["kind"] for i in items}
    assert "record" in kinds
    assert "popup" in kinds
