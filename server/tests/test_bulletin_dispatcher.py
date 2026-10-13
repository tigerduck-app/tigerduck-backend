"""The anonymous bulletin fan-out (`server/bulletins/dispatcher.py`): a
bulletin with more devices than one statement binds, and a bulletin whose
fan-out fails."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, insert, literal, select

from server.bulletins import dispatcher
from server.bulletins.dispatcher import dispatch_pending_bulletins
from server.bulletins.models import (
    FAILED_BULLETIN_RETRY_DELAY,
    Bulletin,
    BulletinDispatch,
    BulletinDispatchStatus,
    BulletinProcessingState,
    BulletinSubscription,
)
from server.bulletins.taxonomy import CanonicalOrg, ContentTag
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


async def test_a_bulletin_whose_fan_out_fails_holds_up_no_other(
    db_session, prepared_engine, test_settings, monkeypatch
):
    """Each tick starts with the oldest bulletin. One whose fan-out raises is
    left to retry once its delay has passed, and the bulletins after it go
    out."""
    await _subscribe_devices(db_session, 1)
    failing = _bulletin("910001", content_tags=[ContentTag.exam.value])
    later = _bulletin("910002")
    db_session.add_all([failing, later])
    await db_session.commit()
    factory = build_session_factory(prepared_engine)
    real_match = dispatcher.match_device_ids

    async def match_device_ids(session, *, canonical_org, content_tags):
        if ContentTag.exam.value in content_tags:
            raise RuntimeError("matching failed")
        return await real_match(
            session, canonical_org=canonical_org, content_tags=content_tags
        )

    monkeypatch.setattr(dispatcher, "match_device_ids", match_device_ids)
    android = RecordingFcmSender()
    outcome = await dispatch_pending_bulletins(factory, _router(android), test_settings)

    assert outcome.bulletins_handled == 1
    assert [r.data["bulletin_id"] for r in android.requests] == [str(later.id)]
    assert not await _notified(db_session, failing)
    assert await _notified(db_session, later)

    monkeypatch.undo()
    android = RecordingFcmSender()
    await dispatch_pending_bulletins(factory, _router(android), test_settings)

    assert android.requests == []

    await dispatch_pending_bulletins(
        factory, _router(android), test_settings,
        now=datetime.now(UTC) + FAILED_BULLETIN_RETRY_DELAY,
    )

    assert [r.data["bulletin_id"] for r in android.requests] == [str(failing.id)]
    assert await _notified(db_session, failing)


async def test_a_failing_bulletin_leaves_its_batch_slot_to_the_next(
    db_session, prepared_engine, test_settings, monkeypatch
):
    """With a batch of one, a bulletin that fails would take the slot of every
    tick; skipped until its retry, it lets the next bulletin out."""
    await _subscribe_devices(db_session, 1)
    failing = _bulletin("910001", content_tags=[ContentTag.exam.value])
    later = _bulletin("910002")
    db_session.add_all([failing, later])
    await db_session.commit()
    factory = build_session_factory(prepared_engine)
    real_match = dispatcher.match_device_ids

    async def match_device_ids(session, *, canonical_org, content_tags):
        if ContentTag.exam.value in content_tags:
            raise RuntimeError("matching failed")
        return await real_match(
            session, canonical_org=canonical_org, content_tags=content_tags
        )

    monkeypatch.setattr(dispatcher, "match_device_ids", match_device_ids)
    android = RecordingFcmSender()
    first = await dispatch_pending_bulletins(
        factory, _router(android), test_settings, batch_limit=1
    )
    second = await dispatch_pending_bulletins(
        factory, _router(android), test_settings, batch_limit=1
    )

    assert (first.bulletins_handled, second.bulletins_handled) == (0, 1)
    assert [r.data["bulletin_id"] for r in android.requests] == [str(later.id)]
    assert not await _notified(db_session, failing)
