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
