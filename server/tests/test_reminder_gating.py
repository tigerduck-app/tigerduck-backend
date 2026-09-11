"""Who an assignment reminder is allowed to reach.

Android keeps local scheduling (spec 4.2), so a backend-delivered
reminder on an Android device is a double notification, not a fallback.
Today FcmService's else-branch happens to drop these for lack of a
bulletin_id -- that is a coincidence, and the first person to add a
handler branch there would ship the bug. The gate belongs here.

The version cases matter just as much: the backend ships before the
clients do, and a 2.0.2 iPhone still runs AssignmentReminderScheduler.
"""

# platform and switches
# 1. an iPhone on 2.1.0 with both flags on receives it
# 2. an iPad on 2.1.0 with both flags on receives it
# 3. an Android device on 2.1.0 with both flags on receives NOTHING
# 4. an iPhone on 2.1.0 with cloud_sync_enabled=False receives NOTHING
# 5. an iPhone on 2.1.0 with sync_assignment_reminders=False receives NOTHING
#
# backward compatibility -- the reason this task has a version gate at all
# 6. an iPhone on 2.0.2 receives NOTHING (it schedules its own)
# 7. an iPhone with app_version NULL receives NOTHING (fail closed)
# 8. an iPhone on 2.2.0 DOES receive it (the gate is >=, not ==)
#
# scope -- proves the gate is not global
# 9. a bulletin-channel job still reaches the Android device
# 10. a bulletin-channel job still reaches the 2.0.2 iPhone

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from server.auth.models import (
    DevicePushToken,
    PushDelivery,
    PushJob,
    User,
    UserDevice,
)
from server.db import build_session_factory
from server.push.apns_client import SendResult
from server.push.pipeline import PushPipelineWorker, run_push_tick
from server.push.reminders import CHANNEL as REMINDER_CHANNEL
from server.push.router import PushRouter

pytestmark = pytest.mark.asyncio(loop_scope="session")


class ScriptedSender:
    """PushSender double that always reports success.

    Duplicated from `test_push_pipeline.py` rather than shared: every push
    test module in this repo keeps its own transport double (same note in
    `test_reauth_push.py`) -- a shared one would couple suites that assert
    on different things.
    """

    def __init__(self):
        self.requests = []

    async def send(self, request):
        self.requests.append(request)
        return SendResult(success=True, status="200")

    async def send_multi(self, requests):
        return [await self.send(r) for r in requests]

    async def close(self):
        pass


def _worker(prepared_engine, test_settings):
    return PushPipelineWorker(
        session_factory=build_session_factory(prepared_engine),
        settings=test_settings,
        router=PushRouter(apple=ScriptedSender(), android=ScriptedSender()),
        worker_id="reminder-gating-test-worker",
    )


async def _device_with_token(
    session,
    user,
    *,
    client_device_id="dev-1",
    platform="ios",
    app_version="2.1.0",
    cloud_sync_enabled=True,
    sync_assignment_reminders=True,
):
    device = UserDevice(
        user_id=user.id,
        client_device_id=client_device_id,
        platform=platform,
        app_version=app_version,
        cloud_sync_enabled=cloud_sync_enabled,
        sync_assignment_reminders=sync_assignment_reminders,
    )
    session.add(device)
    await session.flush()
    session.add(
        DevicePushToken(
            device_id=device.id,
            provider="fcm" if platform == "android" else "apns",
            token_kind="standard",
            token_hash=f"hash-{client_device_id}",
            token_value=f"tok-{client_device_id}",
            bundle_id="org.ntust.app.TigerDuck",
        )
    )
    return device


def _job(user, **kwargs):
    defaults = dict(
        user_id=user.id,
        dedupe_key="test:dedupe:default",
        channel=REMINDER_CHANNEL,
        scenario="reminder_24h",
        fire_at=datetime.now(UTC) - timedelta(minutes=5),
        available_at=datetime.now(UTC) - timedelta(minutes=5),
        payload={"title": "t", "body": "b"},
    )
    defaults.update(kwargs)
    return PushJob(**defaults)


async def _delivered_device_ids(session) -> set:
    rows = (await session.execute(select(PushDelivery))).scalars().all()
    return {row.device_id for row in rows}


@pytest.mark.parametrize(
    "case,platform,app_version,cloud_sync_enabled,sync_assignment_reminders,expect_delivered",
    [
        ("1-iphone-2.1.0-both-on", "ios", "2.1.0", True, True, True),
        ("2-ipad-2.1.0-both-on", "ipados", "2.1.0", True, True, True),
        ("3-android-2.1.0-both-on", "android", "2.1.0", True, True, False),
        ("4-iphone-cloud-sync-off", "ios", "2.1.0", False, True, False),
        ("5-iphone-reminders-off", "ios", "2.1.0", True, False, False),
        ("6-iphone-2.0.2", "ios", "2.0.2", True, True, False),
        ("7-iphone-version-null", "ios", None, True, True, False),
        ("8-iphone-2.2.0", "ios", "2.2.0", True, True, True),
    ],
)
async def test_assignment_reminder_gate(
    db_session,
    prepared_engine,
    test_settings,
    case,
    platform,
    app_version,
    cloud_sync_enabled,
    sync_assignment_reminders,
    expect_delivered,
):
    user = User(student_id="b11203058")
    db_session.add(user)
    await db_session.flush()
    device = await _device_with_token(
        db_session,
        user,
        platform=platform,
        app_version=app_version,
        cloud_sync_enabled=cloud_sync_enabled,
        sync_assignment_reminders=sync_assignment_reminders,
    )
    job = _job(user, dedupe_key=f"assignment:test:{case}")
    db_session.add(job)
    await db_session.commit()

    worker = _worker(prepared_engine, test_settings)
    await run_push_tick(worker)

    delivered = await _delivered_device_ids(db_session)
    assert (device.id in delivered) is expect_delivered, case


@pytest.mark.parametrize(
    "case,platform,app_version",
    [
        ("9-bulletin-android", "android", "2.1.0"),
        ("10-bulletin-old-iphone", "ios", "2.0.2"),
    ],
)
async def test_bulletin_channel_is_not_gated(
    db_session, prepared_engine, test_settings, case, platform, app_version
):
    user = User(student_id="b11203058")
    db_session.add(user)
    await db_session.flush()
    device = await _device_with_token(
        db_session, user, platform=platform, app_version=app_version
    )
    job = _job(
        user,
        channel="bulletin",
        scenario="bulletin_post",
        dedupe_key=f"bulletin:test:{case}",
    )
    db_session.add(job)
    await db_session.commit()

    worker = _worker(prepared_engine, test_settings)
    await run_push_tick(worker)

    delivered = await _delivered_device_ids(db_session)
    assert device.id in delivered, case
