"""POST /v3/sync-jobs/run-now — pull-to-refresh."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from server.auth.moodle import MoodleVerifyResult, StaticMoodleVerifier
from server.db import build_session_factory
from server.syncjobs.models import SyncJob

pytestmark = pytest.mark.asyncio(loop_scope="session")

LOGIN_BODY = {
    "student_id": "b11203058",
    "password": "pw",
    "moodle_token": "tok",
    "moodle_private_token": None,
    "device_info": {
        "client_device_id": "dev-1",
        "platform": "ios",
        "device_name": "iPhone",
        "app_version": "3.0.0",
        "os_version": "26.0",
    },
}


async def _login(client) -> dict:
    client.app.state.moodle_verifier = StaticMoodleVerifier(
        MoodleVerifyResult(ok=True, username="b11203058")
    )
    response = await client.post("/v3/auth/login", json=LOGIN_BODY)
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _get_job(client, job_type: str = "moodle_assignments") -> SyncJob:
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        return (
            await session.execute(
                select(SyncJob).where(SyncJob.job_type == job_type)
            )
        ).scalar_one()


async def _set_job(client, job_type: str = "moodle_assignments", **values):
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        job = (
            await session.execute(
                select(SyncJob).where(SyncJob.job_type == job_type)
            )
        ).scalar_one()
        for key, value in values.items():
            setattr(job, key, value)
        await session.commit()


async def test_run_now_queues_high_priority(client):
    headers = await _login(client)
    # Make the job look like a normal scheduled one in the future.
    await _set_job(
        client,
        run_after=datetime.now(UTC) + timedelta(hours=8),
        cursor={},
    )
    response = await client.post(
        "/v3/sync-jobs/run-now?job_type=moodle_assignments", headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["queued"] is True
    assert body["status"] == "pending"
    assert body["priority"] == 1

    job = await _get_job(client)
    assert job.priority == 1
    assert job.run_after <= datetime.now(UTC)
    assert "manual_requested_at" in job.cursor


async def test_run_now_cooldown_429(client):
    headers = await _login(client)
    await _set_job(
        client, run_after=datetime.now(UTC) + timedelta(hours=8), cursor={}
    )
    first = await client.post(
        "/v3/sync-jobs/run-now?job_type=moodle_assignments", headers=headers
    )
    assert first.status_code == 200

    await _set_job(client, run_after=datetime.now(UTC) + timedelta(hours=8))
    second = await client.post(
        "/v3/sync-jobs/run-now?job_type=moodle_assignments", headers=headers
    )
    assert second.status_code == 429
    assert second.json()["detail"]["error"] == "cooldown"


async def test_run_now_running_job_returns_status_without_requeue(client):
    headers = await _login(client)
    await _set_job(
        client,
        status="running",
        locked_by="worker-1",
        locked_at=datetime.now(UTC),
    )
    response = await client.post(
        "/v3/sync-jobs/run-now?job_type=moodle_assignments", headers=headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["queued"] is False
    assert body["status"] == "running"


async def test_run_now_disabled_returns_credential_invalid(client):
    headers = await _login(client)
    await _set_job(client, status="disabled", last_error="credential_invalid")
    response = await client.post(
        "/v3/sync-jobs/run-now?job_type=moodle_assignments", headers=headers
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "credential_invalid"


async def test_run_now_revives_failed_job(client):
    headers = await _login(client)
    await _set_job(
        client,
        status="failed",
        attempts=3,
        last_error="sync_failed:net",
        cursor={},
    )
    response = await client.post(
        "/v3/sync-jobs/run-now?job_type=moodle_assignments", headers=headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["queued"] is True
    assert body["last_error_code"] == "sync_failed"

    job = await _get_job(client)
    assert job.status == "pending"
    assert job.attempts == 0


async def test_run_now_rejects_unknown_job_type(client):
    headers = await _login(client)
    response = await client.post(
        "/v3/sync-jobs/run-now?job_type=grades", headers=headers
    )
    assert response.status_code == 422  # not in HANDLED_JOB_TYPES literal


async def test_run_now_requires_auth(client):
    response = await client.post(
        "/v3/sync-jobs/run-now?job_type=moodle_assignments"
    )
    assert response.status_code == 401

async def test_run_now_no_job_and_invalid_credentials_409_without_provisioning(
    client,
):
    """With no sync_jobs row and an invalid account credential, run-now
    must 409 instead of provisioning a fresh pending job the executor
    would only re-disable next tick."""
    from server.auth.models import ExternalAccount
    from server.sync.models import UserCourse  # noqa: F401 — model registry

    headers = await _login(client)
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        jobs = (await session.execute(select(SyncJob))).scalars().all()
        for j in jobs:
            await session.delete(j)
        account = (
            await session.execute(select(ExternalAccount))
        ).scalar_one()
        account.credential_status = "invalid"
        await session.commit()

    response = await client.post(
        "/v3/sync-jobs/run-now?job_type=moodle_assignments", headers=headers
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "credential_invalid"

    async with factory() as session:
        jobs = (
            await session.execute(
                select(SyncJob).where(SyncJob.job_type == "moodle_assignments")
            )
        ).scalars().all()
        assert jobs == []

async def test_run_now_disabled_policy_409_without_burning_cooldown(client):
    """Greptile #4: with the policy disabled the executor never claims the
    job — run-now must 409 up front instead of queueing + burning cooldown."""
    from server.syncjobs.models import SyncPolicy

    headers = await _login(client)
    await _set_job(
        client, run_after=datetime.now(UTC) + timedelta(hours=8), cursor={}
    )
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        policy = (
            await session.execute(
                select(SyncPolicy).where(
                    SyncPolicy.job_type == "moodle_assignments"
                )
            )
        ).scalar_one()
        policy.enabled = False
        await session.commit()

    try:
        response = await client.post(
            "/v3/sync-jobs/run-now?job_type=moodle_assignments", headers=headers
        )
        assert response.status_code == 409
        assert response.json()["detail"]["error"] == "policy_disabled"

        job = await _get_job(client)
        assert job.priority != 1  # not queued
        assert "manual_requested_at" not in (job.cursor or {})  # no cooldown burned
    finally:
        async with factory() as session:
            policy = (
                await session.execute(
                    select(SyncPolicy).where(
                        SyncPolicy.job_type == "moodle_assignments"
                    )
                )
            ).scalar_one()
            policy.enabled = True
            await session.commit()
