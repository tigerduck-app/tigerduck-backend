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

from server.auth.models import DevicePushToken, PushJob, PushJobStatus
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
                    PushJob.dedupe_key.like(f"la_end:%:{ACTIVITY_ID}")
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
                    PushJob.dedupe_key.like(f"la_end:%:{ACTIVITY_ID}")
                )
            )
        ).scalar_one()
        assert job.payload["source_id"] == "slot-live"


async def test_register_refuses_an_activity_id_that_is_not_the_composed_one(client) -> None:
    """The pipeline files the start under "{scenario}::{source_id}" and looks
    the running activity up by the same id; a client that registered under
    any other could be started twice and never ended."""
    login = await _login(client)
    target = datetime.now(timezone.utc) + timedelta(minutes=15)
    body = _register_body(target, "d" * 128)
    body["activity_id"] = "classPreparing::somewhere-else"
    response = await client.post(
        "/v3/live-activities/register", headers=_bearer(login), json=body
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "activity_id_mismatch"


async def test_full_sync_does_not_cancel_the_activity_end_job(client) -> None:
    """A full sync cancels this device's pending data-freshness pushes, but
    must leave the Live Activity end job alone.

    The app full-syncs on every foreground, so a sweep that took the end
    job with it cancelled every end within seconds of registration: the
    end push never fired and the Dynamic Island sat on an expired
    countdown showing "—" until iOS's own multi-hour cleanup. Observed in
    production as six consecutive end jobs, all cancelled, none sent.

    The start job in the same sweep is the control: it still gets
    cancelled, so this asserts the exemption is narrow rather than a
    disabled sweep.
    """
    login = await _login(client)
    now = datetime.now(timezone.utc)

    # A pending start job for this device — the thing the sweep is for.
    schedule = await client.post(
        "/v3/schedule/sync",
        headers=_bearer(login),
        json={
            "events": [
                {
                    "source_id": "slot-sweep",
                    "scenario": "classPreparing",
                    "fire_at": _iso(now + timedelta(minutes=30)),
                    "snapshot": _snapshot(now + timedelta(minutes=45)),
                }
            ]
        },
    )
    assert schedule.status_code == 200, schedule.text

    # A pending end job for a running activity.
    target = now + timedelta(minutes=15)
    register = await client.post(
        "/v3/live-activities/register",
        headers=_bearer(login),
        json=_register_body(target, "e" * 128),
    )
    assert register.status_code == 200, register.text
    end_job_id = register.json()["end_job_id"]
    assert end_job_id is not None

    full = await client.get("/v3/sync/full", headers=_bearer(login))
    assert full.status_code == 200, full.text

    factory = build_session_factory(client.app.state.engine)
    async with factory() as s:
        end_job = (
            await s.execute(select(PushJob).where(PushJob.id == end_job_id))
        ).scalar_one()
        assert end_job.status == PushJobStatus.pending.value
        assert end_job.cancelled_at is None
        assert end_job.fire_at == target

        start_jobs = (
            await s.execute(
                select(PushJob).where(
                    PushJob.dedupe_key.like("schedule:%:slot-sweep:%")
                )
            )
        ).scalars().all()
        assert start_jobs, "expected the schedule sync to have filed a start job"
        assert all(
            j.status == PushJobStatus.cancelled.value for j in start_jobs
        ), [j.status for j in start_jobs]


@pytest.mark.parametrize(
    "finished", [PushJobStatus.sent, PushJobStatus.partial_failed]
)
async def test_an_end_job_already_sent_does_not_block_the_next_one(
    client, finished
) -> None:
    """An activity whose id already had its end push sent still gets an
    end job when it runs again.

    `ux_push_jobs_dedupe_active` covers sent and partial_failed rows as
    well as pending ones, so the earlier end held the key: the upsert
    conflicted with it, its update only applies to pending rows, and the
    route answered 200 having filed nothing. The activity then stayed on
    screen past its countdown. Seen after a class was run early under the
    debug clock and then again on the day; an assignment id repeats the
    same way whenever that assignment gets a second activity.
    """
    login = await _login(client)
    now = datetime.now(timezone.utc)
    first = await client.post(
        "/v3/live-activities/register",
        headers=_bearer(login),
        json=_register_body(now + timedelta(minutes=15), "f" * 128),
    )
    assert first.status_code == 200, first.text
    earlier_id = first.json()["end_job_id"]

    factory = build_session_factory(client.app.state.engine)
    async with factory() as s:
        earlier = await s.get(PushJob, earlier_id)
        end_key = earlier.dedupe_key
        earlier.status = finished.value
        await s.commit()

    target = now + timedelta(minutes=45)
    second = await client.post(
        "/v3/live-activities/register",
        headers=_bearer(login),
        json=_register_body(target, "g" * 128),
    )
    assert second.status_code == 200, second.text
    end_job_id = second.json()["end_job_id"]
    assert end_job_id is not None
    assert end_job_id != earlier_id

    async with factory() as s:
        end_job = await s.get(PushJob, end_job_id)
        assert end_job.status == PushJobStatus.pending.value
        assert end_job.fire_at == target
        # The pipeline's already-running check looks the end job up by this
        # exact key, so the new one has to hold it.
        assert end_job.dedupe_key == end_key
        earlier = await s.get(PushJob, earlier_id)
        assert earlier.status == finished.value
        assert earlier.dedupe_key != end_key
