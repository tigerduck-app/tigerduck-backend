"""Resolution logic shared by /custom-push/preview, /custom-push send,
and the custom-push dispatcher."""

import pytest
from sqlalchemy import insert

from server.models import DeviceRegistration
from server.push.custom_push_targeting import (
    TargetFilter,
    count_by_class,
    resolve_target_device_ids,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _seed(session, **overrides):
    defaults = {
        "device_id": "d-default",
        "user_id": "u",
        "platform": "apple",
        "pts_token_hex": "tok",
        "device_token_hex": None,
        "bundle_id": "org.ntust.app.TigerDuck",
        "attrs_type": "TigerDuckActivityAttributes",
        "apns_env": "development",
        "device_class": "iphone",
        "server_push_enabled": True,
    }
    defaults.update(overrides)
    await session.execute(insert(DeviceRegistration).values(**defaults))
    await session.commit()


async def test_target_iphone_matches_only_iphone(db_session):
    await _seed(db_session, device_id="p1", device_class="iphone")
    await _seed(db_session, device_id="p2", device_class="ipad")
    ids = await resolve_target_device_ids(
        db_session, TargetFilter(target_classes=["iphone"])
    )
    assert ids == ["p1"]


async def test_target_iphone_and_ipad_matches_both(db_session):
    await _seed(db_session, device_id="p1", device_class="iphone")
    await _seed(db_session, device_id="p2", device_class="ipad")
    await _seed(
        db_session, device_id="a1", platform="android",
        device_class="android", attrs_type="", apns_env="",
    )
    ids = sorted(
        await resolve_target_device_ids(
            db_session, TargetFilter(target_classes=["iphone", "ipad"])
        )
    )
    assert ids == ["p1", "p2"]


async def test_legacy_empty_apple_matches_both_iphone_and_ipad(db_session):
    await _seed(db_session, device_id="legacy-a", device_class="")
    iphone_only = await resolve_target_device_ids(
        db_session, TargetFilter(target_classes=["iphone"])
    )
    ipad_only = await resolve_target_device_ids(
        db_session, TargetFilter(target_classes=["ipad"])
    )
    assert iphone_only == ["legacy-a"]
    assert ipad_only == ["legacy-a"]


async def test_legacy_empty_apple_does_not_match_android_target(db_session):
    await _seed(db_session, device_id="legacy-a", device_class="")
    ids = await resolve_target_device_ids(
        db_session, TargetFilter(target_classes=["android"])
    )
    assert ids == []


async def test_server_push_disabled_excluded(db_session):
    await _seed(db_session, device_id="p1", server_push_enabled=False)
    ids = await resolve_target_device_ids(
        db_session, TargetFilter(target_classes=["iphone"])
    )
    assert ids == []


async def test_empty_token_excluded(db_session):
    await _seed(db_session, device_id="p1", pts_token_hex="")
    ids = await resolve_target_device_ids(
        db_session, TargetFilter(target_classes=["iphone"])
    )
    assert ids == []


async def test_user_id_filter(db_session):
    await _seed(db_session, device_id="p1", user_id="u1")
    await _seed(db_session, device_id="p2", user_id="u2")
    ids = await resolve_target_device_ids(
        db_session,
        TargetFilter(target_classes=["iphone"], user_id="u2"),
    )
    assert ids == ["p2"]


async def test_device_id_filter(db_session):
    await _seed(db_session, device_id="p1")
    await _seed(db_session, device_id="p2")
    ids = await resolve_target_device_ids(
        db_session,
        TargetFilter(target_classes=["iphone"], device_id="p1"),
    )
    assert ids == ["p1"]


async def test_count_by_class_basic(db_session):
    await _seed(db_session, device_id="p1", device_class="iphone")
    await _seed(db_session, device_id="p2", device_class="iphone")
    await _seed(db_session, device_id="p3", device_class="ipad")
    counts = await count_by_class(
        db_session, TargetFilter(target_classes=["iphone", "ipad"])
    )
    assert counts == {"iphone": 2, "ipad": 1, "total": 3}
