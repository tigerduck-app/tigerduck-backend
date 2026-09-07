"""v3 Live Activity update-token registration tests.

The v2 device-scoped endpoint is retired; /v3/live-activities/register is
auth-scoped and upserts a DevicePushToken (kind=live_activity_update) plus
an "end" PushJob at countdown_target.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from server.auth.models import DevicePushToken, PushJob
from server.auth.moodle import MoodleVerifyResult, StaticMoodleVerifier
from server.db import build_session_factory

pytestmark = pytest.mark.asyncio(loop_scope="session")

ACTIVITY_ID = "classPreparing::slot-live"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _snapshot(countdown_target: datetime) -> dict:
    return {
        "scenario": "classPreparing",
        "title": "Algorithms",
        "subtitle": "10:10-12:00",
        "locationText": "T2-401",
        "instructor": "王小明",
        "countdownTarget": _iso(countdown_target),
        "progressStart": None,
        "accentHex": 0x4A90E2,
        "deepLink": None,
        "sourceId": "slot-live",
    }


def _register_body(target: datetime, token_hex: str, snapshot: dict | None = None) -> dict:
    return {
        "activity_id": ACTIVITY_ID,
        "source_id": "slot-live",
        "update_token_hex": token_hex,
        "countdown_target": _iso(target),
        "snapshot": snapshot if snapshot is not None else _snapshot(target),
        "environment": "development",
    }


async def _login(client) -> dict:
    client.app.state.moodle_verifier = StaticMoodleVerifier(
        MoodleVerifyResult(ok=True, username="whatever")
    )
    response = await client.post(
        "/v3/auth/login",
        json={
            "student_id": "B11015000",
            "password": "pw",
            "moodle_token": "tok",
            "device_info": {"client_device_id": "iphone-live", "platform": "ios"},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _bearer(login: dict) -> dict:
    return {"Authorization": f"Bearer {login['access_token']}"}


async def test_register_live_activity_requires_auth(client) -> None:
    target = datetime.now(timezone.utc) + timedelta(minutes=15)
    response = await client.post(
        "/v3/live-activities/register", json=_register_body(target, "b" * 128)
    )
    assert response.status_code == 401


async def test_register_live_activity_token_upserts(client) -> None:
    login = await _login(client)
    target = datetime.now(timezone.utc) + timedelta(minutes=15)

    first = await client.post(
        "/v3/live-activities/register",
        headers=_bearer(login),
        json=_register_body(target, "b" * 128),
    )
    assert first.status_code == 200, first.text
    assert first.json()["end_job_id"] is not None

    second_target = target + timedelta(minutes=5)
    second = await client.post(
        "/v3/live-activities/register",
        headers=_bearer(login),
        json=_register_body(second_target, "c" * 128),
    )
    assert second.status_code == 200, second.text

    factory = build_session_factory(client.app.state.engine)
    async with factory() as s:
        tokens = (
            await s.execute(
                select(DevicePushToken).where(
                    DevicePushToken.token_kind == "live_activity_update",
                    DevicePushToken.scope_key == ACTIVITY_ID,
                )
            )
        ).scalars().all()
        by_status = {t.status: t for t in tokens}
        # Re-registering with a new token invalidates the previous one and
        # leaves exactly one active row pointing at the new token.
        assert set(by_status) == {"active", "invalidated"}
        assert (
            by_status["active"].token_hash
            == hashlib.sha256(b"c" * 128).hexdigest()
        )

        # The "end" push job is deduped per activity: the second register
        # moves fire_at instead of queueing a second job.
        jobs = (
            await s.execute(
                select(PushJob).where(
                    PushJob.dedupe_key == f"la_end:{ACTIVITY_ID}"
                )
            )
        ).scalars().all()
        assert len(jobs) == 1
        assert jobs[0].fire_at == second_target
        assert jobs[0].payload["kind"] == "live_activity_end"


async def test_end_job_source_id_comes_from_top_level_field(client) -> None:
    """The end-push payload must reference the top-level source_id even if
    the client snapshot carries a divergent sourceId — the cancel_by_source
    and end-push flows key off source_id, so divergence would strand the
    activity (the invariant the old v2 endpoint enforced with a 422)."""
    login = await _login(client)
    target = datetime.now(timezone.utc) + timedelta(minutes=15)
    snapshot = _snapshot(target)
    snapshot["sourceId"] = "slot-other"  # deliberately mismatched

    response = await client.post(
        "/v3/live-activities/register",
        headers=_bearer(login),
        json=_register_body(target, "b" * 128, snapshot=snapshot),
    )
    assert response.status_code == 200, response.text

    factory = build_session_factory(client.app.state.engine)
    async with factory() as s:
        job = (
            await s.execute(
                select(PushJob).where(
                    PushJob.dedupe_key == f"la_end:{ACTIVITY_ID}"
                )
            )
        ).scalar_one()
        assert job.payload["source_id"] == "slot-live"
