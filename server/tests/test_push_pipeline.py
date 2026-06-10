"""Push pipeline: stale recovery, claim, materialize, deliver, aggregate."""

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
from server.push.router import PushRouter

pytestmark = pytest.mark.asyncio(loop_scope="session")


class ScriptedSender:
    """PushSender double returning queued results (last one repeats)."""

    def __init__(self, results=None):
        self.results = list(results or [])
        self.requests = []

    async def send(self, request):
        self.requests.append(request)
        if len(self.results) > 1:
            return self.results.pop(0)
        return (
            self.results[0]
            if self.results
            else SendResult(success=True, status="200")
        )

    async def send_multi(self, requests):
        return [await self.send(r) for r in requests]

    async def close(self):
        pass


def _worker(prepared_engine, test_settings, apple=None, android=None):
    return PushPipelineWorker(
        session_factory=build_session_factory(prepared_engine),
        settings=test_settings,
        router=PushRouter(
            apple=apple or ScriptedSender(), android=android or ScriptedSender()
        ),
        worker_id="push-test-worker",
    )


async def _setup_user_device_token(
    session,
    *,
    platform="ios",
    provider="apns",
    token_value="tok-1",
    token_kwargs=None,
):
    user = User(student_id="b11203058")
    session.add(user)
    await session.flush()
    device = UserDevice(
        user_id=user.id, client_device_id="dev-1", platform=platform
    )
    session.add(device)
    await session.flush()
    token = DevicePushToken(
        device_id=device.id,
        provider=provider,
        token_kind="standard",
        token_hash=f"hash-{token_value}",
        token_value=token_value,
        bundle_id="org.ntust.app.TigerDuck",
        **(token_kwargs or {}),
    )
    session.add(token)
    await session.flush()
    return user, device, token


def _job(user, **kwargs):
    defaults = dict(
        user_id=user.id,
        dedupe_key=f"system:test:{kwargs.get('scenario', 's')}",
        channel="system",
        scenario="reauth_required",
        fire_at=datetime.now(UTC) - timedelta(seconds=1),
        # Explicit past value — the column's server_default now() is the
        # INSERT transaction timestamp, which can land microseconds after
        # the tick's claim cutoff and flake the test.
        available_at=datetime.now(UTC) - timedelta(seconds=1),
        payload={"title": "t", "body": "b"},
    )
    defaults.update(kwargs)
    return PushJob(**defaults)


async def test_due_job_claimed_and_sent(
    db_session, prepared_engine, test_settings
):
    user, _, _ = await _setup_user_device_token(db_session)
    job = _job(user)
    db_session.add(job)
    await db_session.commit()

    apple = ScriptedSender([SendResult(success=True, status="200")])
    worker = _worker(prepared_engine, test_settings, apple=apple)
    await run_push_tick(worker)

    assert len(apple.requests) == 1
    await db_session.refresh(job)
    assert job.status == "sent"
    assert job.sent_at is not None
    delivery = (
        await db_session.execute(
            select(PushDelivery).where(PushDelivery.push_job_id == job.id)
        )
    ).scalar_one()
    assert delivery.status == "sent"
    assert delivery.provider == "apns"


async def test_future_or_unavailable_jobs_not_claimed(
    db_session, prepared_engine, test_settings
):
    user, _, _ = await _setup_user_device_token(db_session)
    db_session.add(
        _job(user, fire_at=datetime.now(UTC) + timedelta(hours=1), scenario="f")
    )
    db_session.add(
        _job(
            user,
            available_at=datetime.now(UTC) + timedelta(minutes=5),
            scenario="a",
            dedupe_key="system:test:a2",
        )
    )
    await db_session.commit()

    apple = ScriptedSender()
    worker = _worker(prepared_engine, test_settings, apple=apple)
    await run_push_tick(worker)
    assert apple.requests == []


async def test_stale_processing_job_recovered_with_backoff(
    db_session, prepared_engine, test_settings
):
    user, _, _ = await _setup_user_device_token(db_session)
    job = _job(
        user,
        status="processing",
        locked_by="dead",
        locked_at=datetime.now(UTC) - timedelta(minutes=6),
        available_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    db_session.add(job)
    await db_session.commit()

    worker = _worker(prepared_engine, test_settings)
    await run_push_tick(worker)

    await db_session.refresh(job)
    # attempts++ and pushed back to pending with future available_at...
    # then NOT claimed this tick (available_at in the future).
    assert job.attempts == 1
    assert job.status == "pending"
    assert job.available_at > datetime.now(UTC)


async def test_stale_processing_exhausted_goes_failed(
    db_session, prepared_engine, test_settings
):
    user, _, _ = await _setup_user_device_token(db_session)
    job = _job(
        user,
        status="processing",
        locked_by="dead",
        locked_at=datetime.now(UTC) - timedelta(minutes=6),
        attempts=2,
        max_attempts=3,
    )
    db_session.add(job)
    await db_session.commit()

    worker = _worker(prepared_engine, test_settings)
    await run_push_tick(worker)
    await db_session.refresh(job)
    assert job.status == "failed"


async def test_fanout_to_all_devices_and_partial_failed(
    db_session, prepared_engine, test_settings
):
    user, _, _ = await _setup_user_device_token(db_session)
    device2 = UserDevice(
        user_id=user.id, client_device_id="dev-2", platform="android"
    )
    db_session.add(device2)
    await db_session.flush()
    db_session.add(
        DevicePushToken(
            device_id=device2.id,
            provider="fcm",
            token_kind="standard",
            token_hash="hash-fcm-1",
            token_value="fcm-1",
        )
    )
    job = _job(user)
    db_session.add(job)
    await db_session.commit()

    apple = ScriptedSender([SendResult(success=True, status="200")])
    android = ScriptedSender(
        [SendResult(success=False, status="UNKNOWN", description="boom")] * 4
    )
    worker = _worker(prepared_engine, test_settings, apple=apple, android=android)

    # Round 1: apns sent, fcm transient-pending → job re-queued.
    await run_push_tick(worker)
    await db_session.refresh(job)
    assert job.status == "pending"
    assert job.attempts == 1

    # Exhaust remaining rounds (delivery max_attempts=3, job max_attempts=3).
    for _ in range(4):
        job.available_at = datetime.now(UTC) - timedelta(seconds=1)
        await db_session.commit()
        await run_push_tick(worker)
        await db_session.refresh(job)
        if job.status != "pending":
            break
    assert job.status == "partial_failed"
    deliveries = (
        (
            await db_session.execute(
                select(PushDelivery).where(PushDelivery.push_job_id == job.id)
            )
        )
        .scalars()
        .all()
    )
    assert {d.status for d in deliveries} == {"sent", "failed"}


async def test_unregistered_token_invalidated(
    db_session, prepared_engine, test_settings
):
    user, _, token = await _setup_user_device_token(db_session)
    job = _job(user)
    db_session.add(job)
    await db_session.commit()

    apple = ScriptedSender(
        [SendResult(success=False, status="410", description="Unregistered")]
    )
    worker = _worker(prepared_engine, test_settings, apple=apple)
    await run_push_tick(worker)

    await db_session.refresh(job)
    assert job.status == "failed"
    await db_session.refresh(token)
    assert token.status == "invalidated"
    delivery = (
        await db_session.execute(
            select(PushDelivery).where(PushDelivery.push_job_id == job.id)
        )
    ).scalar_one()
    assert delivery.failure_code == "unregistered"


async def test_no_active_tokens_fails_job(
    db_session, prepared_engine, test_settings
):
    user, _, token = await _setup_user_device_token(
        db_session, token_kwargs={"status": "invalidated"}
    )
    job = _job(user)
    db_session.add(job)
    await db_session.commit()

    worker = _worker(prepared_engine, test_settings)
    await run_push_tick(worker)
    await db_session.refresh(job)
    assert job.status == "failed"
    assert job.last_error == "no_active_tokens"


async def test_device_targeted_job_only_hits_that_device(
    db_session, prepared_engine, test_settings
):
    user, device1, _ = await _setup_user_device_token(db_session)
    device2 = UserDevice(
        user_id=user.id, client_device_id="dev-2", platform="ios"
    )
    db_session.add(device2)
    await db_session.flush()
    db_session.add(
        DevicePushToken(
            device_id=device2.id,
            provider="apns",
            token_kind="standard",
            token_hash="hash-tok-2",
            token_value="tok-2",
        )
    )
    job = _job(user, device_id=device2.id)
    db_session.add(job)
    await db_session.commit()

    apple = ScriptedSender([SendResult(success=True, status="200")])
    worker = _worker(prepared_engine, test_settings, apple=apple)
    await run_push_tick(worker)

    assert len(apple.requests) == 1
    assert apple.requests[0].device_token == "tok-2"


async def test_live_activity_tokens_excluded(
    db_session, prepared_engine, test_settings
):
    user, device, _ = await _setup_user_device_token(db_session)
    db_session.add(
        DevicePushToken(
            device_id=device.id,
            provider="apns",
            token_kind="live_activity_update",
            token_hash="hash-la",
            token_value="la-tok",
            scope_key="assignment:1",
        )
    )
    job = _job(user)
    db_session.add(job)
    await db_session.commit()

    apple = ScriptedSender([SendResult(success=True, status="200")])
    worker = _worker(prepared_engine, test_settings, apple=apple)
    await run_push_tick(worker)
    assert len(apple.requests) == 1  # standard token only


async def test_all_skipped_after_token_dies_between_rounds(
    db_session, prepared_engine, test_settings
):
    user, _, token = await _setup_user_device_token(db_session)
    job = _job(user)
    db_session.add(job)
    await db_session.commit()

    apple = ScriptedSender(
        [SendResult(success=False, status="UNKNOWN", description="blip")] * 2
    )
    worker = _worker(prepared_engine, test_settings, apple=apple)

    # Round 1: transient failure → delivery stays pending, job re-queued.
    await run_push_tick(worker)
    await db_session.refresh(job)
    assert job.status == "pending"

    # Token dies between rounds; drive remaining rounds (also pull the
    # delivery's retry backoff into the past so the round attempts it).
    token.status = "invalidated"
    pending_delivery = (
        await db_session.execute(
            select(PushDelivery).where(PushDelivery.push_job_id == job.id)
        )
    ).scalar_one()
    for _ in range(4):
        job.available_at = datetime.now(UTC) - timedelta(seconds=1)
        pending_delivery.next_retry_at = datetime.now(UTC) - timedelta(
            seconds=1
        )
        await db_session.commit()
        await run_push_tick(worker)
        await db_session.refresh(job)
        await db_session.refresh(pending_delivery)
        if job.status != "pending":
            break

    assert job.status == "failed"
    assert job.last_error == "all_tokens_skipped"
    delivery = (
        await db_session.execute(
            select(PushDelivery).where(PushDelivery.push_job_id == job.id)
        )
    ).scalar_one()
    assert delivery.status == "skipped"


async def test_exhausted_delivery_keeps_last_transport_error(
    db_session, prepared_engine, test_settings
):
    user, _, _ = await _setup_user_device_token(db_session)
    job = _job(user)
    db_session.add(job)
    await db_session.commit()

    apple = ScriptedSender(
        [SendResult(success=False, status="UNKNOWN", description="boom")] * 5
    )
    worker = _worker(prepared_engine, test_settings, apple=apple)
    for _ in range(5):
        job.available_at = datetime.now(UTC) - timedelta(seconds=1)
        await db_session.commit()
        await run_push_tick(worker)
        await db_session.refresh(job)
        if job.status != "pending":
            break

    assert job.status == "failed"
    delivery = (
        await db_session.execute(
            select(PushDelivery)
            .where(PushDelivery.push_job_id == job.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert delivery.status == "failed"
    # The real transport error survives — not overwritten by
    # retries_exhausted bookkeeping.
    assert delivery.failure_code == "UNKNOWN"
