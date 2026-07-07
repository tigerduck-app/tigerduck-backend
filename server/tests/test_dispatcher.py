"""Dispatcher tests — uses RecordingSender so nothing hits APNs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from server.config import Settings
from server.db import build_session_factory
from server.models import (
    DeviceRegistration,
    LiveActivityTokenStatus,
    LiveActivityUpdateToken,
    PushStatus,
    ScheduledPush,
    build_push_id,
)
from server.push.apns_client import RecordingSender, SendResult
from server.scheduler.dispatcher import dispatch_due_pushes

pytestmark = pytest.mark.asyncio(loop_scope="session")


DEVICE_ID = "dev-dispatcher"


async def _register_device(client: AsyncClient) -> None:
    """The v2 register endpoint is retired (410); insert the anonymous
    device_registrations row the dispatcher fans out to directly."""
    factory = build_session_factory(client.app.state.engine)
    async with factory() as s:
        s.add(
            DeviceRegistration(
                user_id="user-dispatcher",
                device_id=DEVICE_ID,
                pts_token_hex="aa" * 80,
                device_token_hex="bb" * 32,
                bundle_id="org.ntust.app.TigerDuck",
                attrs_type="TigerDuckActivityAttributes",
                apns_env="development",
            )
        )
        await s.commit()


async def _seed_push(
    factory: async_sessionmaker,
    *,
    source_id: str,
    scenario: str,
    fire_at: datetime,
    status: str = PushStatus.pending.value,
    attempts: int = 0,
) -> str:
    push_id = build_push_id(DEVICE_ID, source_id, scenario)
    async with factory() as s:
        push = ScheduledPush(
            push_id=push_id,
            device_id=DEVICE_ID,
            source_id=source_id,
            scenario=scenario,
            fire_at=fire_at,
            payload_json={
                "scenario": scenario,
                "title": "Algorithms",
                "subtitle": "10:10-12:00",
                "locationText": "T2-401",
                "instructor": "王小明",
                "countdownTarget": fire_at.isoformat(),
                "progressStart": None,
                "accentHex": 0x4A90E2,
                "deepLink": None,
                "sourceId": source_id,
            },
            status=status,
            attempts=attempts,
        )
        s.add(push)
        await s.commit()
    return push_id


async def _status_of(factory: async_sessionmaker, push_id: str) -> tuple[str, int, str | None]:
    async with factory() as s:
        row = await s.get(ScheduledPush, push_id)
        assert row is not None
        return row.status, row.attempts, row.last_error


async def _seed_live_activity_token(
    factory: async_sessionmaker,
    *,
    activity_id: str,
    source_id: str,
    scenario: str,
    countdown_target: datetime,
    status: str = LiveActivityTokenStatus.active.value,
    attempts: int = 0,
) -> str:
    async with factory() as s:
        token = LiveActivityUpdateToken(
            activity_id=activity_id,
            device_id=DEVICE_ID,
            source_id=source_id,
            scenario=scenario,
            update_token_hex="cc" * 80,
            countdown_target=countdown_target,
            snapshot_json={
                "scenario": scenario,
                "title": "Algorithms",
                "subtitle": "10:10-12:00",
                "locationText": "T2-401",
                "instructor": "王小明",
                "countdownTarget": countdown_target.isoformat(),
                "progressStart": None,
                "accentHex": 0x4A90E2,
                "deepLink": None,
                "sourceId": source_id,
            },
            status=status,
            attempts=attempts,
        )
        s.add(token)
        await s.commit()
    return activity_id


async def _activity_status_of(
    factory: async_sessionmaker,
    activity_id: str,
) -> tuple[str, int, str | None]:
    async with factory() as s:
        row = await s.get(LiveActivityUpdateToken, activity_id)
        assert row is not None
        return row.status, row.attempts, row.last_error


async def test_due_push_is_sent(client: AsyncClient, prepared_engine: AsyncEngine, test_settings: Settings):
    await _register_device(client)
    factory = build_session_factory(prepared_engine)

    now = datetime.now(timezone.utc)
    push_id = await _seed_push(
        factory,
        source_id="slot-a",
        scenario="classPreparing",
        fire_at=now,  # due right now
    )

    sender = RecordingSender()
    outcome = await dispatch_due_pushes(factory, sender, test_settings, now=now)

    assert outcome.dispatched == 1
    assert outcome.sent == 1
    assert len(sender.requests) == 1

    # Verify the payload is built with the Live Activity topic + PTS token
    req = sender.requests[0]
    assert req.topic.endswith(".push-type.liveactivity")
    assert req.priority == 10
    assert req.message["aps"]["event"] == "start"

    status, attempts, err = await _status_of(factory, push_id)
    assert status == PushStatus.sent.value
    assert attempts == 1
    assert err is None


async def test_future_push_is_skipped(client: AsyncClient, prepared_engine: AsyncEngine, test_settings: Settings):
    await _register_device(client)
    factory = build_session_factory(prepared_engine)
    now = datetime.now(timezone.utc)
    # Push far beyond the window (scheduler_window_seconds defaults to 60s)
    push_id = await _seed_push(
        factory,
        source_id="slot-future",
        scenario="classPreparing",
        fire_at=now + timedelta(minutes=10),
    )

    sender = RecordingSender()
    outcome = await dispatch_due_pushes(factory, sender, test_settings, now=now)

    assert outcome.dispatched == 0
    assert len(sender.requests) == 0

    status, _, _ = await _status_of(factory, push_id)
    assert status == PushStatus.pending.value


class _FailingSender:
    """Sender that always returns transient failures."""

    async def send(self, request) -> SendResult:
        return SendResult(success=False, status="500", description="simulated")

    async def close(self) -> None:
        pass


class _BadTokenSender:
    async def send(self, request) -> SendResult:
        return SendResult(success=False, status="410", description="BadDeviceToken")

    async def close(self) -> None:
        pass


async def test_transient_failure_bumps_attempts_then_fails(
    client: AsyncClient, prepared_engine: AsyncEngine, test_settings: Settings
):
    await _register_device(client)
    factory = build_session_factory(prepared_engine)
    now = datetime.now(timezone.utc)
    push_id = await _seed_push(
        factory, source_id="slot-retry", scenario="classPreparing", fire_at=now
    )

    sender = _FailingSender()

    # Tick 1 → attempts = 1, still pending
    await dispatch_due_pushes(factory, sender, test_settings, now=now)
    status, attempts, err = await _status_of(factory, push_id)
    assert status == PushStatus.pending.value
    assert attempts == 1
    assert "simulated" in (err or "")

    # Tick 2 → attempts = 2, still pending
    await dispatch_due_pushes(factory, sender, test_settings, now=now)
    status, attempts, _ = await _status_of(factory, push_id)
    assert status == PushStatus.pending.value
    assert attempts == 2

    # Tick 3 → attempts = 3 → marked failed
    await dispatch_due_pushes(factory, sender, test_settings, now=now)
    status, attempts, _ = await _status_of(factory, push_id)
    assert status == PushStatus.failed.value
    assert attempts == 3


async def test_expired_event_is_cancelled_not_sent(
    client: AsyncClient, prepared_engine: AsyncEngine, test_settings: Settings
):
    """Fix #5: if the snapshot's countdownTarget is already past, don't waste
    an APNs call — APNs would drop it anyway (apns-expiration=countdownTarget).
    Mark as cancelled so monitoring counts it correctly."""
    await _register_device(client)
    factory = build_session_factory(prepared_engine)

    now = datetime.now(timezone.utc)
    past = now - timedelta(minutes=30)
    push_id = build_push_id(DEVICE_ID, "slot-past", "inClass")

    # Seed a push whose countdownTarget is already 30 min in the past
    async with factory() as s:
        push = ScheduledPush(
            push_id=push_id,
            device_id=DEVICE_ID,
            source_id="slot-past",
            scenario="inClass",
            fire_at=now,  # fire_at due
            payload_json={
                "scenario": "inClass",
                "title": "Stale",
                "subtitle": "09:10-10:00",
                "sourceId": "slot-past",
                "countdownTarget": past.isoformat(),
            },
            status=PushStatus.pending.value,
            attempts=0,
        )
        s.add(push)
        await s.commit()

    sender = RecordingSender()
    outcome = await dispatch_due_pushes(factory, sender, test_settings, now=now)

    assert outcome.cancelled == 1
    assert outcome.sent == 0
    # No APNs call should have happened
    assert sender.requests == []

    status, _, err = await _status_of(factory, push_id)
    assert status == PushStatus.cancelled.value
    assert "event_expired" in (err or "")


async def test_bad_token_prunes_device_and_cancels(
    client: AsyncClient, prepared_engine: AsyncEngine, test_settings: Settings
):
    await _register_device(client)
    factory = build_session_factory(prepared_engine)
    now = datetime.now(timezone.utc)
    push_id = await _seed_push(
        factory, source_id="slot-bad", scenario="classPreparing", fire_at=now
    )

    sender = _BadTokenSender()
    outcome = await dispatch_due_pushes(factory, sender, test_settings, now=now)

    assert outcome.cancelled == 1

    # The FK ON DELETE CASCADE means pruning the device also removes its
    # queued pushes. Both the device and the push row should be gone.
    async with factory() as s:
        dev = await s.get(DeviceRegistration, DEVICE_ID)
        assert dev is None
        push = await s.get(ScheduledPush, push_id)
        assert push is None


async def test_due_live_activity_end_is_sent(
    client: AsyncClient,
    prepared_engine: AsyncEngine,
    test_settings: Settings,
):
    await _register_device(client)
    factory = build_session_factory(prepared_engine)
    now = datetime.now(timezone.utc)
    activity_id = await _seed_live_activity_token(
        factory,
        activity_id="inClass::slot-end",
        source_id="slot-end",
        scenario="inClass",
        countdown_target=now,
    )

    sender = RecordingSender()
    outcome = await dispatch_due_pushes(factory, sender, test_settings, now=now)

    assert outcome.dispatched == 1
    assert outcome.sent == 1
    assert len(sender.requests) == 1
    req = sender.requests[0]
    assert req.device_token == "cc" * 80
    assert req.message["aps"]["event"] == "end"
    assert req.message["aps"]["dismissal-date"] == int(now.timestamp())

    status, attempts, err = await _activity_status_of(factory, activity_id)
    assert status == LiveActivityTokenStatus.ended.value
    assert attempts == 1
    assert err is None


async def test_live_activity_end_within_window_is_sent(
    client: AsyncClient,
    prepared_engine: AsyncEngine,
    test_settings: Settings,
):
    """Near-future Live Activity ends fire the same tick, matching the push
    path. `scheduler_window_seconds` (default 60) is the symmetric look-ahead."""
    await _register_device(client)
    factory = build_session_factory(prepared_engine)
    now = datetime.now(timezone.utc)
    activity_id = await _seed_live_activity_token(
        factory,
        activity_id="inClass::slot-near-end",
        source_id="slot-near-end",
        scenario="inClass",
        countdown_target=now + timedelta(seconds=30),
    )

    sender = RecordingSender()
    outcome = await dispatch_due_pushes(factory, sender, test_settings, now=now)

    assert outcome.dispatched == 1
    assert outcome.sent == 1
    status, _, _ = await _activity_status_of(factory, activity_id)
    assert status == LiveActivityTokenStatus.ended.value


async def test_future_live_activity_end_is_skipped(
    client: AsyncClient,
    prepared_engine: AsyncEngine,
    test_settings: Settings,
):
    await _register_device(client)
    factory = build_session_factory(prepared_engine)
    now = datetime.now(timezone.utc)
    activity_id = await _seed_live_activity_token(
        factory,
        activity_id="inClass::slot-future-end",
        source_id="slot-future-end",
        scenario="inClass",
        countdown_target=now + timedelta(minutes=10),
    )

    sender = RecordingSender()
    outcome = await dispatch_due_pushes(factory, sender, test_settings, now=now)

    assert outcome.dispatched == 0
    assert sender.requests == []
    status, _, _ = await _activity_status_of(factory, activity_id)
    assert status == LiveActivityTokenStatus.active.value


# --- Phase 4d: v3 live-activity update tokens take over (v2 fallback) ---


async def _link_device_with_v3_token(
    factory: async_sessionmaker,
    *,
    scope_key: str,
    token_value: str,
    token_status: str = "active",
) -> None:
    """Mark the v2 device as linked to a v3 user owning a
    live_activity_update push token with `scope_key`."""
    from server.auth.models import DevicePushToken, User, UserDevice

    async with factory() as s:
        user = User(student_id="B11015888")
        s.add(user)
        await s.flush()
        device = UserDevice(
            user_id=user.id, client_device_id=DEVICE_ID, platform="ios"
        )
        s.add(device)
        await s.flush()
        s.add(
            DevicePushToken(
                device_id=device.id,
                provider="apns",
                token_kind="live_activity_update",
                token_hash=f"hash-{token_value[:16]}",
                token_value=token_value,
                bundle_id="org.ntust.app.TigerDuck",
                scope_key=scope_key,
                status=token_status,
            )
        )
        reg = await s.get(DeviceRegistration, DEVICE_ID)
        reg.linked_user_id = user.id
        await s.commit()


async def test_linked_device_prefers_v3_update_token(
    client: AsyncClient,
    prepared_engine: AsyncEngine,
    test_settings: Settings,
):
    await _register_device(client)
    factory = build_session_factory(prepared_engine)
    now = datetime.now(timezone.utc)
    await _seed_live_activity_token(
        factory,
        activity_id="inClass::slot-v3",
        source_id="slot-v3",
        scenario="inClass",
        countdown_target=now,
    )
    await _link_device_with_v3_token(
        factory, scope_key="inClass:slot-v3", token_value="ee" * 80
    )

    sender = RecordingSender()
    outcome = await dispatch_due_pushes(factory, sender, test_settings, now=now)

    assert outcome.sent == 1
    assert sender.requests[0].device_token == "ee" * 80  # v3 token wins


async def test_linked_device_without_matching_scope_falls_back(
    client: AsyncClient,
    prepared_engine: AsyncEngine,
    test_settings: Settings,
):
    await _register_device(client)
    factory = build_session_factory(prepared_engine)
    now = datetime.now(timezone.utc)
    await _seed_live_activity_token(
        factory,
        activity_id="inClass::slot-other",
        source_id="slot-other",
        scenario="inClass",
        countdown_target=now,
    )
    # v3 token exists but for a DIFFERENT activity scope.
    await _link_device_with_v3_token(
        factory, scope_key="inClass:some-other-slot", token_value="ee" * 80
    )

    sender = RecordingSender()
    outcome = await dispatch_due_pushes(factory, sender, test_settings, now=now)

    assert outcome.sent == 1
    assert sender.requests[0].device_token == "cc" * 80  # v2 fallback


async def test_invalidated_v3_update_token_ignored(
    client: AsyncClient,
    prepared_engine: AsyncEngine,
    test_settings: Settings,
):
    await _register_device(client)
    factory = build_session_factory(prepared_engine)
    now = datetime.now(timezone.utc)
    await _seed_live_activity_token(
        factory,
        activity_id="inClass::slot-dead",
        source_id="slot-dead",
        scenario="inClass",
        countdown_target=now,
    )
    await _link_device_with_v3_token(
        factory,
        scope_key="inClass:slot-dead",
        token_value="ee" * 80,
        token_status="invalidated",
    )

    sender = RecordingSender()
    outcome = await dispatch_due_pushes(factory, sender, test_settings, now=now)

    assert outcome.sent == 1
    assert sender.requests[0].device_token == "cc" * 80  # v2 fallback
