"""When Bulletin.dispatch_filter_json is set, the dispatcher targets the
device set returned by TargetFilter resolution and bypasses the per-device
subscription matcher entirely. Also confirms that the payload builders
receive `kind="custom_push_bulletin"` and the filter's `force_ring`.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from server.bulletins.dispatcher import dispatch_pending_bulletins
from server.bulletins.models import (
    Bulletin,
    BulletinDispatch,
    BulletinDispatchStatus,
    BulletinProcessingState,
)
from server.config import Settings
from server.models import DevicePlatform, DeviceRegistration
from server.push.apns_client import RecordingSender
from server.push.fcm_client import RecordingFcmSender
from server.push.router import PushRouter

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_dispatch_filter_json_bypasses_subscription_matcher(
    prepared_engine: AsyncEngine, test_settings: Settings
) -> None:
    factory = async_sessionmaker(prepared_engine, expire_on_commit=False)

    # Three devices: two iphones (apple) + one android. None have any
    # BulletinSubscription rules, so the legacy matcher would return 0
    # — the custom-push path must still resolve the iphones.
    seed_rows = [
        ("p1", "iphone", DevicePlatform.apple.value, "a1" * 32),
        ("p2", "iphone", DevicePlatform.apple.value, "a2" * 32),
        ("a1", "android", DevicePlatform.android.value, None),
    ]
    async with factory() as session:
        for did, cls, plat, dtok in seed_rows:
            session.add(
                DeviceRegistration(
                    device_id=did,
                    user_id="u",
                    platform=plat,
                    pts_token_hex="tok-" + did,
                    device_token_hex=dtok,
                    bundle_id="org.ntust.app.TigerDuck",
                    attrs_type=(
                        "TigerDuckActivityAttributes"
                        if plat == "apple"
                        else ""
                    ),
                    apns_env="development" if plat == "apple" else "",
                    device_class=cls,
                    server_push_enabled=True,
                )
            )
        bulletin = Bulletin(
            source="custom_push",
            external_id="custom-1",
            source_url="",
            title="t",
            title_clean="t",
            body_md="b",
            body_clean="b",
            summary="b",
            canonical_org="server",
            content_tags=["server_notification"],
            importance="normal",
            processing_state=BulletinProcessingState.processed.value,
            dispatch_filter_json={
                "target_classes": ["iphone"],
                "user_id": None,
                "device_id": None,
                "force_ring": True,
            },
        )
        session.add(bulletin)
        await session.commit()
        bulletin_id = bulletin.id

    apple = RecordingSender()
    android = RecordingFcmSender()
    router = PushRouter(apple=apple, android=android)
    try:
        await dispatch_pending_bulletins(factory, router, test_settings)
    finally:
        await router.close()

    # Both iphones reached `sent` for our bulletin (no subscription rules
    # exist — the legacy matcher would have returned 0; the custom path
    # resolves both iPhones, skips the Android).
    async with factory() as session:
        rows = (
            await session.execute(
                select(BulletinDispatch).where(
                    BulletinDispatch.bulletin_id == bulletin_id
                )
            )
        ).scalars().all()
        rows_by_did = {r.device_id: r for r in rows}
        assert "p1" in rows_by_did and "p2" in rows_by_did
        assert "a1" not in rows_by_did  # android excluded by target_classes
        assert rows_by_did["p1"].status == BulletinDispatchStatus.sent.value
        assert rows_by_did["p2"].status == BulletinDispatchStatus.sent.value

    # Apple sender received both iPhone tokens for *this* bulletin with
    # kind=custom_push_bulletin + force_ring=true (alias `default` sound).
    apple_for_bulletin = [
        r for r in apple.requests if r.message.get("bulletin_id") == bulletin_id
    ]
    tokens_seen = {r.device_token for r in apple_for_bulletin}
    assert {"a1" * 32, "a2" * 32}.issubset(tokens_seen)
    for req in apple_for_bulletin:
        assert req.message["kind"] == "custom_push_bulletin"
        assert req.message["force_ring"] == "true"
        assert req.message["aps"].get("sound") == "default"

    # Android sender saw nothing for this bulletin.
    assert all(
        r.data.get("bulletin_id") != str(bulletin_id) for r in android.requests
    )


async def test_dispatch_filter_json_force_ring_false_silences_apple(
    prepared_engine: AsyncEngine, test_settings: Settings
) -> None:
    """With force_ring=False, the APNs payload omits `aps.sound` so iOS
    delivers the push silently."""
    factory = async_sessionmaker(prepared_engine, expire_on_commit=False)

    async with factory() as session:
        session.add(
            DeviceRegistration(
                device_id="p-silent",
                user_id="u",
                platform=DevicePlatform.apple.value,
                pts_token_hex="tok-silent",
                # Distinct device_token_hex so we can filter the recording
                # router's history away from leftover rows from sibling
                # tests sharing the session-scoped prepared_engine.
                device_token_hex="cd" * 32,
                bundle_id="org.ntust.app.TigerDuck",
                attrs_type="TigerDuckActivityAttributes",
                apns_env="development",
                device_class="iphone",
                server_push_enabled=True,
            )
        )
        bulletin = Bulletin(
            source="custom_push",
            external_id="custom-2",
            source_url="",
            title="t2",
            title_clean="t2",
            body_md="b2",
            body_clean="b2",
            summary="b2",
            canonical_org="server",
            content_tags=["server_notification"],
            importance="normal",
            processing_state=BulletinProcessingState.processed.value,
            dispatch_filter_json={
                "target_classes": ["iphone"],
                "user_id": None,
                "device_id": None,
                "force_ring": False,
            },
        )
        session.add(bulletin)
        await session.commit()
        bulletin_id = bulletin.id

    apple = RecordingSender()
    android = RecordingFcmSender()
    router = PushRouter(apple=apple, android=android)
    try:
        await dispatch_pending_bulletins(factory, router, test_settings)
    finally:
        await router.close()

    apple_for_silent_device = [
        r for r in apple.requests if r.device_token == "cd" * 32
    ]
    assert len(apple_for_silent_device) == 1
    req = apple_for_silent_device[0]
    assert req.message["bulletin_id"] == bulletin_id
    assert req.message["kind"] == "custom_push_bulletin"
    assert req.message["force_ring"] == "false"
    assert "sound" not in req.message["aps"]
