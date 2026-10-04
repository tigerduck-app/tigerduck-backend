"""APNs client factory tests — does NOT hit Apple's servers."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from aioapns import APNs
from aioapns.common import NotificationResult
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from server.config import Settings
from server.push.apns_client import (
    AioApnsSender,
    RecordingSender,
    build_sender,
)
from server.push.job_payloads import build_apns_for_job


def _no_apns_settings() -> Settings:
    return Settings(apns_team_id="", apns_key_id="")


def _fake_apns_settings(key_path) -> Settings:
    return Settings(
        apns_team_id="TEAM1234AB",
        apns_key_id="KEY1234ABC",
        apns_key_path=key_path,
    )


def test_build_sender_returns_recording_when_unconfigured():
    sender = build_sender(_no_apns_settings())
    assert isinstance(sender, RecordingSender)


def test_build_sender_errors_when_key_missing(tmp_path):
    missing = tmp_path / "does_not_exist.p8"
    # key_id/team_id set but file missing → falls back to recording
    sender = build_sender(_fake_apns_settings(missing))
    assert isinstance(sender, RecordingSender)


def test_aioapns_sender_rejects_partial_config(tmp_path):
    # key_id set, team_id missing → AioApnsSender() itself should raise
    bad = Settings(apns_team_id="", apns_key_id="KEY1234ABC")
    with pytest.raises(ValueError):
        AioApnsSender(bad)


@pytest.mark.asyncio
async def test_recording_sender_captures_requests():
    sender = RecordingSender()
    request = build_apns_for_job(
        payload={
            "kind": "live_activity_end",
            "activity_id": "classPreparing:slot-777",
            "source_id": "slot-777",
            "scenario": "classPreparing",
            "title": "Algorithms",
            "subtitle": "10:10-12:00",
            "locationText": "T2-401",
            "sourceId": "slot-777",
        },
        channel="schedule",
        token_value="deadbeef" * 8,
        bundle_id="org.ntust.app.TigerDuck",
        now=datetime(2026, 4, 22, 2, 45, tzinfo=timezone.utc),
    )
    result = await sender.send(request)
    assert result.success is True
    assert sender.requests == [request]


def _p8_settings(tmp_path, *, apns_env: str) -> Settings:
    """Settings for a real AioApnsSender on a throwaway .p8 key. Building
    the sender is offline; nothing here opens a connection."""
    key = ec.generate_private_key(ec.SECP256R1())
    path = tmp_path / "AuthKey_TEST.p8"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return Settings(
        apns_team_id="TEAM1234AB",
        apns_key_id="KEY1234ABC",
        apns_key_path=path,
        apns_env=apns_env,
    )


def _alert():
    return build_apns_for_job(
        payload={"kind": "bulletin", "title": "t", "body": "b", "bulletin_id": 1},
        channel="bulletin",
        token_value="ab" * 32,
        bundle_id="org.ntust.app.TigerDuck",
        now=datetime(2026, 10, 4, 2, 0, tzinfo=timezone.utc),
    )


async def test_an_apns_connection_stays_open_across_push_ticks(tmp_path, monkeypatch):
    # Apple: reuse a connection for hours, and an open-close-open pattern can
    # get the server temporarily blocked as a denial-of-service. aioapns
    # closes a connection after 10s idle on its own, so with the pipeline
    # ticking every 30s nearly every tick with Apple work reconnected.
    idle: list[int] = []

    async def fake_send_notification(self, notification):
        idle.append(self.pool.protocol_class.INACTIVITY_TIME)
        return NotificationResult(notification_id="apns-id", status="200")

    monkeypatch.setattr(APNs, "send_notification", fake_send_notification)
    settings = _p8_settings(tmp_path, apns_env="development").model_copy(
        update={"apns_connection_idle_seconds": 600}
    )
    sender = AioApnsSender(settings)

    await sender.send(_alert())
    await sender.send(_alert())

    assert idle == [600, 600]
    assert min(idle) > settings.push_pipeline_tick_seconds


async def test_a_send_apns_never_answers_gives_up_and_reconnects(tmp_path, monkeypatch):
    # aioapns awaits the response with no timeout, and a connection that died
    # without a FIN is not reported lost until TCP gives up — minutes, during
    # which the push tick (which never overlaps itself) would stall.
    clients: list[int] = []

    async def fake_send_notification(self, notification):
        clients.append(id(self))
        if len(clients) == 1:
            await asyncio.Event().wait()  # the answer that never comes
        return NotificationResult(notification_id="apns-id", status="200")

    monkeypatch.setattr(APNs, "send_notification", fake_send_notification)
    settings = _p8_settings(tmp_path, apns_env="development").model_copy(
        update={"apns_send_timeout_seconds": 0.05}
    )
    sender = AioApnsSender(settings)

    stuck = await asyncio.wait_for(sender.send(_alert()), timeout=2)
    after = await asyncio.wait_for(sender.send(_alert()), timeout=2)

    assert (stuck.success, stuck.status) == (False, "TIMEOUT")
    assert after.success is True
    # The next send does not queue behind the dead connection.
    assert clients[0] != clients[1]
