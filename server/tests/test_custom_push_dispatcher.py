"""The custom-push dispatcher drains pending custom_push_dispatches and
routes them to APNs (apple devices) or FCM (android devices)."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from server.config import Settings
from server.db import build_session_factory
from server.models import (
    CustomPushDispatch,
    CustomPushStatus,
    DeviceRegistration,
)
from server.push.apns_client import RecordingSender, SendResult
from server.push.custom_push_dispatcher import dispatch_pending_custom_pushes
from server.push.fcm_client import RecordingFcmSender
from server.push.router import PushRouter

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(loop_scope="session")
async def session_factory(prepared_engine: AsyncEngine):
    """Per-test session factory against a freshly reset schema.

    Mirrors the `client` fixture's drop/create-all pattern so each test
    in this module starts from an empty schema (the bulletin dispatcher
    test uses an unscoped sessionmaker without a reset, which can leak
    rows across tests sharing the session-scoped engine)."""
    from server.db import Base

    async with prepared_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    return build_session_factory(prepared_engine)


@pytest.fixture
def settings(test_settings: Settings) -> Settings:
    return test_settings


@pytest.fixture
def push_router_recording() -> PushRouter:
    return PushRouter(apple=RecordingSender(), android=RecordingFcmSender())


class _FailingApnsSender:
    async def send(self, request) -> SendResult:
        return SendResult(success=False, status="failed", description="oops")

    async def close(self) -> None:
        pass


class _FailingFcmSender:
    async def send(self, request) -> SendResult:
        return SendResult(success=False, status="failed", description="oops")

    async def send_multi(self, requests) -> list[SendResult]:
        return [
            SendResult(success=False, status="failed", description="oops")
            for _ in requests
        ]

    async def close(self) -> None:
        pass


@pytest.fixture
def push_router_recording_failing() -> PushRouter:
    return PushRouter(apple=_FailingApnsSender(), android=_FailingFcmSender())


async def test_drains_pending_and_sends_to_correct_transport(
    session_factory, push_router_recording, settings
):
    async with session_factory() as s:
        await s.execute(insert(DeviceRegistration).values(
            device_id="p1", user_id="u", platform="apple",
            pts_token_hex="ab" * 32,
            device_token_hex="cd" * 32,
            bundle_id="org.ntust.app.TigerDuck",
            attrs_type="TigerDuckActivityAttributes", apns_env="development",
            device_class="iphone", server_push_enabled=True,
        ))
        await s.execute(insert(DeviceRegistration).values(
            device_id="a1", user_id="u", platform="android",
            pts_token_hex="tok-a1",
            bundle_id="org.ntust.app.TigerDuck",
            attrs_type="", apns_env="",
            device_class="android", server_push_enabled=True,
        ))
        for did in ["p1", "a1"]:
            await s.execute(insert(CustomPushDispatch).values(
                request_id="req-1",
                device_id=did,
                title="hi",
                body="hello",
                force_ring=True,
                notification_id=f"nid-{did}",
                status=CustomPushStatus.pending.value,
                attempts=0,
            ))
        await s.commit()

    await dispatch_pending_custom_pushes(
        session_factory, push_router_recording, settings
    )

    async with session_factory() as s:
        rows = (await s.execute(
            select(CustomPushDispatch)
            .where(CustomPushDispatch.request_id == "req-1")
            .order_by(CustomPushDispatch.device_id)
        )).scalars().all()
        assert all(r.status == CustomPushStatus.sent.value for r in rows)
        assert all(r.sent_at is not None for r in rows)


async def test_failed_send_increments_attempts(
    session_factory, push_router_recording_failing, settings
):
    """When the router fails, attempts increments and status stays pending
    until MAX_ATTEMPTS reached, then flips to failed."""
    async with session_factory() as s:
        await s.execute(insert(DeviceRegistration).values(
            device_id="p-fail", user_id="u", platform="apple",
            pts_token_hex="ff" * 32,
            device_token_hex="ee" * 32,
            bundle_id="org.ntust.app.TigerDuck",
            attrs_type="TigerDuckActivityAttributes", apns_env="development",
            device_class="iphone", server_push_enabled=True,
        ))
        await s.execute(insert(CustomPushDispatch).values(
            request_id="req-fail",
            device_id="p-fail",
            title="t", body="b",
            force_ring=True,
            notification_id="nid-fail",
            status=CustomPushStatus.pending.value,
            attempts=2,  # one more failure → flips to failed
        ))
        await s.commit()

    await dispatch_pending_custom_pushes(
        session_factory, push_router_recording_failing, settings
    )

    async with session_factory() as s:
        row = (await s.execute(
            select(CustomPushDispatch).where(CustomPushDispatch.request_id == "req-fail")
        )).scalar_one()
        assert row.attempts == 3
        assert row.status == CustomPushStatus.failed.value
        assert row.last_error
