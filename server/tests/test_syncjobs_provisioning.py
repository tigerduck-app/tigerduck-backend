"""Login creates/revives sync_jobs (e2e through /v3/auth/login)."""

from __future__ import annotations

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


async def _login(client):
    client.app.state.moodle_verifier = StaticMoodleVerifier(
        MoodleVerifyResult(ok=True, username="b11203058")
    )
    response = await client.post("/v3/auth/login", json=LOGIN_BODY)
    assert response.status_code == 200, response.text
    return response.json()


async def test_login_creates_moodle_assignments_job(client):
    await _login(client)
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        jobs = (await session.execute(select(SyncJob))).scalars().all()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.job_type == "moodle_assignments"
    assert job.status == "pending"
    assert job.max_attempts == 3


async def test_relogin_revives_disabled_job(client):
    await _login(client)
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        job = (await session.execute(select(SyncJob))).scalar_one()
        job.status = "disabled"
        job.attempts = 3
        job.last_error = "credential_invalid"
        await session.commit()

    await _login(client)
    async with factory() as session:
        job = (await session.execute(select(SyncJob))).scalar_one()
    assert job.status == "pending"
    assert job.attempts == 0
    assert job.last_error is None


async def test_relogin_does_not_reset_healthy_job(client):
    await _login(client)
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        job = (await session.execute(select(SyncJob))).scalar_one()
        job.attempts = 2  # mid-backoff but not disabled
        await session.commit()

    await _login(client)
    async with factory() as session:
        job = (await session.execute(select(SyncJob))).scalar_one()
    assert job.attempts == 2  # untouched
