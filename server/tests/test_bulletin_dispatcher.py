"""The anonymous bulletin fan-out (`server/bulletins/dispatcher.py`): a
bulletin with more devices than one statement binds."""

from __future__ import annotations

import pytest
from sqlalchemy import func, insert, literal, select

from server.bulletins.dispatcher import dispatch_pending_bulletins
from server.bulletins.models import (
    Bulletin,
    BulletinDispatch,
    BulletinDispatchStatus,
    BulletinProcessingState,
    BulletinSubscription,
)
from server.bulletins.taxonomy import CanonicalOrg
from server.db import build_session_factory
from server.models import DevicePlatform, DeviceRegistration
from server.push.apns_client import RecordingSender
from server.push.fcm_client import RecordingFcmSender
from server.push.router import PushRouter

pytestmark = pytest.mark.asyncio(loop_scope="session")

# No other test uses it, so devices left behind here match no bulletin of the
# dispatcher tests that share the database without resetting it.
ORG = CanonicalOrg.computer_center.value


def _bulletin(external_id: str, *, content_tags=()) -> Bulletin:
    return Bulletin(
        source="ntust_general",
        external_id=external_id,
        source_url=f"https://bulletin.ntust.edu.tw/{external_id}",
        title="電算中心公告",
        canonical_org=ORG,
        content_tags=list(content_tags),
        processing_state=BulletinProcessingState.processed.value,
    )


async def _subscribe_devices(session, count: int) -> None:
    """`count` anonymous Android devices subscribed to every bulletin of ORG.
    Postgres generates them: built in Python, the rows take seconds."""
    n = func.generate_series(1, count).column_valued("n")
    device = {
        "device_id": func.concat("dev-", n),
        "user_id": literal("u"),
        "platform": literal(DevicePlatform.android.value),
        "pts_token_hex": func.concat("fcm-", n),
        "bundle_id": literal("org.ntust.app.tigerduck"),
        "attrs_type": literal(""),
        "apns_env": literal(""),
    }
    await session.execute(
        insert(DeviceRegistration).from_select(list(device), select(*device.values()))
    )
    subscription = {
        "device_id": DeviceRegistration.device_id,
        "orgs": literal([ORG], BulletinSubscription.orgs.type),
        "tags": literal([], BulletinSubscription.tags.type),
        "mode": literal("AND"),
    }
    await session.execute(
        insert(BulletinSubscription).from_select(
            list(subscription), select(*subscription.values())
        )
    )


def _router(android: RecordingFcmSender) -> PushRouter:
    return PushRouter(apple=RecordingSender(), android=android)


async def _notified(session, bulletin: Bulletin) -> bool:
    await session.refresh(bulletin)
    return bulletin.notified_at is not None


async def test_a_bulletin_reaches_more_devices_than_one_insert_binds(
    db_session, prepared_engine, test_settings
):
    """asyncpg allows 32,767 bind parameters per statement and a dispatch row
    binds 4. Filed in one insert, the rows of a bulletin matched by 8,192
    devices failed the tick, every tick, and every bulletin after it waited.

    All but one device already have a sent row, which keeps the test to one
    push; the insert still carries a row for every device matched.
    """
    await _subscribe_devices(db_session, 8_192)
    bulletin = _bulletin("910001")
    db_session.add(bulletin)
    await db_session.flush()
    sent = {
        "bulletin_id": literal(bulletin.id),
        "device_id": DeviceRegistration.device_id,
        "status": literal(BulletinDispatchStatus.sent.value),
    }
    await db_session.execute(
        insert(BulletinDispatch).from_select(
            list(sent),
            select(*sent.values()).where(DeviceRegistration.device_id != "dev-8192"),
        )
    )
    await db_session.commit()
    android = RecordingFcmSender()

    await dispatch_pending_bulletins(
        build_session_factory(prepared_engine), _router(android), test_settings
    )

    assert [request.token for request in android.requests] == ["fcm-8192"]
    assert await _notified(db_session, bulletin)
