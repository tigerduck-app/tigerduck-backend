"""FcmSender tests — build the real firebase-admin Message, never call Google."""

from __future__ import annotations

import json
from pathlib import Path

import firebase_admin
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from firebase_admin import messaging

from server.config import Settings
from server.push.fcm_client import FcmSender
from server.push.job_payloads import build_fcm_for_job
from server.push.router import build_router

SYNC_TRIGGER = {"kind": "sync_trigger", "source_device_id": None}
BULLETIN = {
    "kind": "bulletin",
    "title": "停課公告",
    "body": "颱風停課",
    "bulletin_id": 42,
    "source_url": "https://www.ntust.edu.tw/p/406-1000-42.php",
    "canonical_org": "oaa",
}


def _service_account(tmp_path) -> Path:
    """A throwaway service account. Building a sender on it is offline —
    firebase-admin only parses the key — and every test replaces the send
    call, so nothing leaves the process."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    path = tmp_path / "service_account.json"
    path.write_text(
        json.dumps(
            {
                "type": "service_account",
                "project_id": "tigerduck-test",
                "private_key_id": "test",
                "private_key": pem,
                "client_email": "fcm@tigerduck-test.iam.gserviceaccount.com",
                "client_id": "1",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        )
    )
    return path


@pytest.fixture
async def fcm_sender(tmp_path):
    sender = FcmSender(_service_account(tmp_path), "tigerduck-test")
    yield sender
    await sender.close()


def _capture_send(monkeypatch) -> list[messaging.Message]:
    sent: list[messaging.Message] = []

    def fake_send(message, dry_run=False, app=None):
        sent.append(message)
        return "projects/tigerduck-test/messages/1"

    monkeypatch.setattr(messaging, "send", fake_send)
    return sent


def _capture_send_each(monkeypatch) -> list[messaging.Message]:
    sent: list[messaging.Message] = []

    def fake_send_each(messages, dry_run=False, app=None):
        sent.extend(messages)
        return messaging.BatchResponse(
            [
                messaging.SendResponse({"name": f"projects/p/messages/{i}"}, None)
                for i, _ in enumerate(messages)
            ]
        )

    monkeypatch.setattr(messaging, "send_each", fake_send_each)
    return sent


async def test_a_sync_trigger_reaches_fcm_at_normal_priority(fcm_sender, monkeypatch):
    # The app shows nothing for a sync trigger. Sent at high priority, a run
    # of them teaches FCM that this app's high-priority messages show
    # nothing, and FCM then demotes them — the ones that do show included.
    sent = _capture_send(monkeypatch)
    request = build_fcm_for_job(
        payload=SYNC_TRIGGER, channel="system", token_value="fcm-tok"
    )

    result = await fcm_sender.send(request)

    assert result.success is True
    assert sent[0].android.priority == "normal"


async def test_a_notification_reaches_fcm_at_high_priority(fcm_sender, monkeypatch):
    sent = _capture_send(monkeypatch)
    request = build_fcm_for_job(
        payload=BULLETIN, channel="bulletin", token_value="fcm-tok"
    )

    await fcm_sender.send(request)

    assert sent[0].android.priority == "high"


async def test_send_multi_keeps_each_requests_priority(fcm_sender, monkeypatch):
    sent = _capture_send_each(monkeypatch)
    requests = [
        build_fcm_for_job(payload=SYNC_TRIGGER, channel="system", token_value="a"),
        build_fcm_for_job(payload=BULLETIN, channel="bulletin", token_value="b"),
    ]

    results = await fcm_sender.send_multi(requests)

    assert [r.success for r in results] == [True, True]
    assert [(m.token, m.android.priority) for m in sent] == [
        ("a", "normal"),
        ("b", "high"),
    ]


async def test_fcm_message_ids_come_back_as_the_notification_id(fcm_sender, monkeypatch):
    # The pipeline records `notification_id` as the delivery's
    # provider_message_id — the one handle for tracing a message with Google.
    _capture_send(monkeypatch)
    _capture_send_each(monkeypatch)
    request = build_fcm_for_job(payload=BULLETIN, channel="bulletin", token_value="a")

    single = await fcm_sender.send(request)
    batch = await fcm_sender.send_multi([request, request])

    assert single.notification_id == "projects/tigerduck-test/messages/1"
    assert [r.notification_id for r in batch] == [
        "projects/p/messages/0",
        "projects/p/messages/1",
    ]


async def test_the_http_timeout_setting_reaches_firebase_admin(tmp_path):
    # firebase-admin reads `httpTimeout` from the app's options when it
    # builds its messaging client; left unset, a hung request runs for 120s
    # on a thread `send` has already given up on.
    settings = Settings(
        fcm_project_id="tigerduck-test",
        fcm_credentials_path=_service_account(tmp_path),
        fcm_http_timeout_seconds=7.0,
    )
    router = build_router(settings)
    try:
        app = firebase_admin.get_app("tigerduck-fcm")
        assert app.options.get("httpTimeout") == 7.0
    finally:
        await router.close()
