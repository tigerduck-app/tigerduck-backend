"""How a client's sync holds back the server's own Moodle sync
(tigerduck-backend#24): an upload of fresh data may delay it, a read may
not, and no client can hold it back past its schedule."""

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
    "moodle_token": "tok",
    "device_info": {
        "client_device_id": "mac-1",
        "platform": "macos",
        "app_version": "3.0.0",
        "os_version": "26.0",
    },
}
ASSIGNMENTS = {
    "assignments": [
        {
            "moodle_assignment_id": 1,
            "course_no": "CS3001301",
            "course_name": "資料結構",
            "title": "HW1",
        }
    ]
}
COURSES = {"courses": [{"semester": "1141", "course_no": "CS3001301", "course_name": "DS"}]}
_PUSHBACK = timedelta(hours=2)


async def _login(client) -> dict:
    client.app.state.moodle_verifier = StaticMoodleVerifier(
        MoodleVerifyResult(ok=True, username="b11203058")
    )
    response = await client.post("/v3/auth/login", json=LOGIN_BODY)
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _schedule(client, *, last_success_ago: timedelta, run_after_in: timedelta):
    """Put every job of the user at the given point of its 8-hour cycle."""
    now = datetime.now(UTC)
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        for job in (await session.execute(select(SyncJob))).scalars():
            job.last_success_at = now - last_success_ago
            job.run_after = now + run_after_in
        await session.commit()


async def _run_after(client, job_type: str = "moodle_assignments") -> datetime:
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        return (
            await session.execute(
                select(SyncJob.run_after).where(SyncJob.job_type == job_type)
            )
        ).scalar_one()


async def test_a_full_sync_leaves_the_server_sync_where_it_was(client):
    """A Mac left open reads /sync/full every few minutes and uploads
    nothing; each read used to push the server's sync two hours out."""
    headers = await _login(client)
    await _schedule(
        client, last_success_ago=timedelta(hours=7), run_after_in=timedelta(hours=1)
    )
    before = await _run_after(client)

    response = await client.get("/v3/sync/full", headers=headers)

    assert response.status_code == 200
    assert await _run_after(client) == before


async def test_an_upload_holds_back_a_sync_due_soon(client):
    headers = await _login(client)
    await _schedule(
        client, last_success_ago=timedelta(hours=7), run_after_in=timedelta(hours=1)
    )

    started = datetime.now(UTC)
    response = await client.post(
        "/v3/sync/assignments/upload", headers=headers, json=ASSIGNMENTS
    )

    assert response.status_code == 200
    assert await _run_after(client) >= started + _PUSHBACK


@pytest.mark.parametrize(
    ("last_success_ago", "run_after_in"),
    [
        # The run is further out than the hold-back: moving it would pull it in.
        (timedelta(hours=2), timedelta(hours=6)),
        # A policy interval since the last success: the run is due, and an
        # Android device uploading every 15 minutes would hold it back forever.
        (timedelta(hours=9), timedelta(minutes=-1)),
        # Held back once already, past its schedule: not again.
        (timedelta(hours=8, minutes=30), timedelta(hours=1)),
    ],
    ids=["later-run", "overdue", "already-held-back"],
)
async def test_an_upload_leaves_a_sync_it_must_not_move(
    client, last_success_ago, run_after_in
):
    headers = await _login(client)
    await _schedule(
        client, last_success_ago=last_success_ago, run_after_in=run_after_in
    )
    before = await _run_after(client)

    response = await client.post(
        "/v3/sync/assignments/upload", headers=headers, json=ASSIGNMENTS
    )

    assert response.status_code == 200
    assert await _run_after(client) == before


async def test_a_course_upload_leaves_the_assignment_sync_alone(client):
    """A course list says nothing about assignments or their submissions."""
    headers = await _login(client)
    await _schedule(
        client, last_success_ago=timedelta(hours=7), run_after_in=timedelta(hours=1)
    )
    before = await _run_after(client)

    response = await client.post(
        "/v3/sync/courses/upload", headers=headers, json=COURSES
    )

    assert response.status_code == 200
    assert await _run_after(client) == before
