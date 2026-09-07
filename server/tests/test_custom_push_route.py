"""DB-level coverage for custom-push behaviors formerly exercised via the
retired /v2/custom-push route.

The HTTP endpoints (preview / send / recent) were deleted in the v2 sunset
(commit 101b4fb) — the admin portal now reads and writes postgres directly
and delivers via v3 push_jobs. What remains live server-side is the
targeting resolver in `server.push.custom_push_targeting` (used by the
bulletin dispatcher for `dispatch_filter_json` bulletins) and the
`custom_push_dispatches` drain in `server.push.custom_push_dispatcher`
(scheduler tick). Those are covered by test_custom_push_targeting.py,
test_custom_push_dispatcher.py, test_payload_custom_push.py and
test_bulletins_dispatcher_custom_filter.py.

This file keeps the two route-era guarantees NOT already covered there:
the three-class preview breakdown (android included) and the
empty-filter-matches-nothing guard.
"""

from __future__ import annotations

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
    # Mirror a real Apple registration: both standard APNs alert token
    # (device_token_hex) and PTS Live-Activity token (pts_token_hex). The
    # custom-push targeting gate requires device_token_hex on Apple.
    # Android callers should override platform + clear apple-specific
    # fields (FCM registration token lives in pts_token_hex).
    defaults = {
        "device_id": "d-default",
        "user_id": "u1",
        "platform": "apple",
        "pts_token_hex": "ab" * 16,
        "device_token_hex": "cd" * 32,
        "bundle_id": "org.ntust.app.TigerDuck",
        "attrs_type": "TigerDuckActivityAttributes",
        "apns_env": "development",
        "device_class": "iphone",
        "server_push_enabled": True,
    }
    defaults.update(overrides)
    await session.execute(insert(DeviceRegistration).values(**defaults))
    await session.commit()


async def test_preview_returns_per_class_counts(db_session):
    # Route-era /v2/custom-push/preview contract across all three device
    # classes: each class is counted separately and total sums them.
    # (test_custom_push_targeting.py only covers iphone/ipad + legacy
    # buckets for count_by_class — this keeps the android class covered.)
    await _seed(db_session, device_id="p1", device_class="iphone")
    await _seed(db_session, device_id="p2", device_class="ipad")
    await _seed(
        db_session,
        device_id="a1",
        platform="android",
        device_class="android",
        device_token_hex=None,
        pts_token_hex="fcm-tok",
        attrs_type="",
        apns_env="",
    )
    counts = await count_by_class(
        db_session, TargetFilter(target_classes=["iphone", "ipad", "android"])
    )
    assert counts == {"iphone": 1, "ipad": 1, "android": 1, "total": 3}


async def test_empty_target_classes_matches_nothing(db_session):
    # The retired route rejected an empty target_classes list with 422 at
    # the schema layer. The guard that remains live server-side is in the
    # resolver: an empty filter must never fan out to (or count) anyone,
    # even when otherwise-matching devices exist — the bulletin dispatcher
    # feeds `dispatch_filter_json` straight into this resolver.
    await _seed(db_session, device_id="p1", device_class="iphone")
    ids = await resolve_target_device_ids(
        db_session, TargetFilter(target_classes=[])
    )
    assert ids == []
    counts = await count_by_class(db_session, TargetFilter(target_classes=[]))
    assert counts == {"total": 0}
