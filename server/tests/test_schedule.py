"""Schedule sync endpoint tests (v3: JWT-scoped, backed by PushJobs)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from server.auth.models import PushJob, PushJobStatus, UserDevice
from server.auth.moodle import MoodleVerifyResult, StaticMoodleVerifier
from server.db import build_session_factory

pytestmark = pytest.mark.asyncio(loop_scope="session")


DEVICE_ID = "device-sched"
STUDENT_ID = "B11015000"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _snapshot(title: str) -> dict:
    return {
        "scenario": "classPreparing",
        "title": title,
        "subtitle": "10:10-12:00",
        "locationText": "T2-401",
        "instructor": "王小明",
        "countdownTarget": _iso(datetime.now(timezone.utc) + timedelta(minutes=30)),
        "progressStart": None,
        "accentHex": 0x4A90E2,
        "deepLink": None,
        "sourceId": "slot-1",
    }


def _event(source_id: str, scenario: str, fire_at: datetime, title: str) -> dict:
    return {
        "source_id": source_id,
        "scenario": scenario,
        "fire_at": _iso(fire_at),
        "snapshot": _snapshot(title),
    }


async def _login(client: AsyncClient) -> dict:
    client.app.state.moodle_verifier = StaticMoodleVerifier(
        MoodleVerifyResult(ok=True, username="whatever")
    )
    response = await client.post(
        "/v3/auth/login",
        json={
            "student_id": STUDENT_ID,
            "password": "pw",
            "moodle_token": "tok",
            "device_info": {"client_device_id": DEVICE_ID, "platform": "ios"},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _bearer(login: dict) -> dict:
    return {"Authorization": f"Bearer {login['access_token']}"}


async def test_sync_requires_authenticated_device(client: AsyncClient):
    """v2 rejected unknown device_ids with 404; v3 scopes the schedule by
    Bearer token, so a device that never logged in is refused with 401."""
    response = await client.post("/v3/schedule/sync", json={"events": []})
    assert response.status_code == 401

    cancel = await client.delete("/v3/schedule/slot-1")
    assert cancel.status_code == 401


async def test_sync_inserts_and_reports_counts(client: AsyncClient):
    login = await _login(client)
    fire = datetime.now(timezone.utc) + timedelta(minutes=15)
    response = await client.post(
        "/v3/schedule/sync",
        headers=_bearer(login),
        json={
            "events": [
                _event("slot-1", "classPreparing", fire, "Intro to CS"),
                _event(
                    "slot-1",
                    "inClass",
                    fire + timedelta(minutes=15),
                    "Intro to CS",
                ),
            ],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["pending"] == 2
    assert body["replaced"] == 0

    # Both scenarios landed as pending schedule PushJobs carrying the snapshot.
    factory = build_session_factory(client.app.state.engine)
    async with factory() as s:
        jobs = (
            (
                await s.execute(
                    select(PushJob).where(PushJob.channel == "schedule")
                )
            )
            .scalars()
            .all()
        )
        # Filed under the device that posted the schedule: each of the
        # user's devices keeps its own.
        device = login["device_id"]
        assert {j.dedupe_key for j in jobs} == {
            f"schedule:{device}:slot-1:classPreparing",
            f"schedule:{device}:slot-1:inClass",
        }
        assert all(j.status == PushJobStatus.pending.value for j in jobs)
        prep = next(j for j in jobs if j.scenario == "classPreparing")
        assert prep.payload["kind"] == "schedule"
        assert prep.payload["source_id"] == "slot-1"
        assert prep.payload["title"] == "Intro to CS"
        # The attribute the start push creates the activity with. Has to be
        # the client's `composedActivityId` ("{scenario}::{sourceId}"),
        # because that is the key the client registers the activity's
        # update token under and the end job is filed by.
        assert prep.payload["activity_id"] == "classPreparing::slot-1"


async def test_sync_replaces_previous_events(client: AsyncClient):
    login = await _login(client)
    fire = datetime.now(timezone.utc) + timedelta(hours=1)

    first = await client.post(
        "/v3/schedule/sync",
        headers=_bearer(login),
        json={
            "events": [
                _event("slot-1", "classPreparing", fire, "A"),
                _event("slot-2", "classPreparing", fire + timedelta(hours=2), "B"),
            ],
        },
    )
    assert first.json()["pending"] == 2

    # Second sync keeps only slot-2, so slot-1 should be cancelled
    second = await client.post(
        "/v3/schedule/sync",
        headers=_bearer(login),
        json={
            "events": [
                _event("slot-2", "classPreparing", fire + timedelta(hours=2), "B"),
            ],
        },
    )
    body = second.json()
    assert body["replaced"] == 1
    assert body["pending"] == 1


async def test_resync_does_not_revive_already_sent_push(
    client: AsyncClient, prepared_engine
):
    """Audit finding N2: a re-sync after delivery must not re-fire the push.

    Reproduce: login, sync 1 event, mark its PushJob sent in-DB (simulating
    pipeline delivery), then re-sync the same event. The job must stay
    `status=sent` and no new pending job may appear for the same dedupe key
    (the ux_push_jobs_dedupe_active partial index covers delivered states).
    """
    from sqlalchemy import update as sa_update

    login = await _login(client)
    fire = datetime.now(timezone.utc) + timedelta(minutes=5)
    dedupe_key = f"schedule:{login['device_id']}:slot-sent:classPreparing"

    await client.post(
        "/v3/schedule/sync",
        headers=_bearer(login),
        json={"events": [_event("slot-sent", "classPreparing", fire, "A")]},
    )

    factory = build_session_factory(prepared_engine)

    # Simulate the push pipeline marking this job as delivered
    async with factory() as s:
        await s.execute(
            sa_update(PushJob)
            .where(PushJob.dedupe_key == dedupe_key)
            .values(status=PushJobStatus.sent.value)
        )
        await s.commit()

    # Re-sync — same dedupe key, same content. Must NOT re-create the job.
    resync = await client.post(
        "/v3/schedule/sync",
        headers=_bearer(login),
        json={"events": [_event("slot-sent", "classPreparing", fire, "A")]},
    )
    assert resync.json() == {"pending": 0, "replaced": 0}

    async with factory() as s:
        jobs = (
            (
                await s.execute(
                    select(PushJob).where(PushJob.dedupe_key == dedupe_key)
                )
            )
            .scalars()
            .all()
        )
        assert len(jobs) == 1, f"expected 1 job after re-sync, got {len(jobs)}"
        assert jobs[0].status == PushJobStatus.sent.value, (
            f"expected sent after re-sync, got {jobs[0].status}"
        )


async def test_cancel_by_source_removes_all_scenarios(client: AsyncClient):
    login = await _login(client)
    fire = datetime.now(timezone.utc) + timedelta(hours=2)

    await client.post(
        "/v3/schedule/sync",
        headers=_bearer(login),
        json={
            "events": [
                _event("slot-1", "classPreparing", fire, "A"),
                _event("slot-1", "inClass", fire + timedelta(minutes=15), "A"),
                _event("slot-2", "classPreparing", fire + timedelta(hours=3), "B"),
            ],
        },
    )

    cancel = await client.delete("/v3/schedule/slot-1", headers=_bearer(login))
    assert cancel.status_code == 200
    assert cancel.json()["cancelled"] == 2

    # Now re-sync with only slot-2 to count the remaining pending jobs
    final = await client.post(
        "/v3/schedule/sync",
        headers=_bearer(login),
        json={
            "events": [
                _event("slot-2", "classPreparing", fire + timedelta(hours=3), "B"),
            ],
        },
    )
    body = final.json()
    assert body["pending"] == 1
    assert body["replaced"] == 0


async def test_sync_leaves_the_end_jobs_alone(client: AsyncClient, prepared_engine):
    """`/live-activities/register` files a running activity's end push on
    the same channel, under `la_end:{activity_id}`. Sweeping the whole
    channel cancelled it on every sync, so the activity never ended and the
    pipeline could not see it running."""
    login = await _login(client)
    fire = datetime.now(timezone.utc) + timedelta(minutes=30)
    factory = build_session_factory(prepared_engine)

    async with factory() as s:
        device = (
            await s.execute(
                select(UserDevice).where(UserDevice.client_device_id == DEVICE_ID)
            )
        ).scalar_one()
        s.add(
            PushJob(
                user_id=device.user_id,
                device_id=device.id,
                dedupe_key=f"la_end:{device.id}:inClass::slot-1",
                channel="schedule",
                scenario="activityEnd",
                fire_at=fire,
                payload={"kind": "live_activity_end", "activity_id": "inClass::slot-1"},
            )
        )
        await s.commit()

    response = await client.post(
        "/v3/schedule/sync",
        headers=_bearer(login),
        json={"events": [_event("slot-2", "classPreparing", fire, "B")]},
    )
    assert response.json() == {"pending": 1, "replaced": 0}

    async with factory() as s:
        end_job = (
            await s.execute(
                select(PushJob).where(PushJob.dedupe_key.like("la_end:%:inClass::slot-1"))
            )
        ).scalar_one()
        assert end_job.status == PushJobStatus.pending.value


async def test_each_device_keeps_its_own_schedule(client: AsyncClient):
    """Two devices on one account post different schedules; neither may
    cancel or shadow the other's. Keyed per user alone, the second sync
    collided on the dedupe index and only the first device ever got a
    start."""
    fire = datetime.now(timezone.utc) + timedelta(hours=1)
    phone = await _login(client)
    client.app.state.moodle_verifier = StaticMoodleVerifier(
        MoodleVerifyResult(ok=True, username="whatever")
    )
    tablet_login = await client.post(
        "/v3/auth/login",
        json={
            "student_id": STUDENT_ID,
            "password": "pw",
            "moodle_token": "tok",
            "device_info": {"client_device_id": "device-tablet", "platform": "ipados"},
        },
    )
    tablet = tablet_login.json()

    first = await client.post(
        "/v3/schedule/sync",
        headers=_bearer(phone),
        json={"events": [_event("slot-1", "classPreparing", fire, "A")]},
    )
    assert first.json() == {"pending": 1, "replaced": 0}

    second = await client.post(
        "/v3/schedule/sync",
        headers=_bearer(tablet),
        json={"events": [_event("slot-1", "classPreparing", fire, "A")]},
    )
    # A job of its own, and nothing of the phone's replaced.
    assert second.json() == {"pending": 1, "replaced": 0}

    # The response counts alone cannot tell this from the old per-user key,
    # where the second insert silently collided and reported the same
    # numbers: there must be two rows, one per device.
    factory = build_session_factory(client.app.state.engine)
    async with factory() as s:
        jobs = (
            (
                await s.execute(
                    select(PushJob).where(
                        PushJob.channel == "schedule",
                        PushJob.status == PushJobStatus.pending.value,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(jobs) == 2
    assert len({j.device_id for j in jobs}) == 2
    assert {j.dedupe_key.split(":")[1] for j in jobs} == {phone["device_id"], tablet["device_id"]}

    # Cancelling by source on the tablet leaves the phone's job standing.
    cancel = await client.delete("/v3/schedule/slot-1", headers=_bearer(tablet))
    assert cancel.json()["cancelled"] == 1
    phone_again = await client.post(
        "/v3/schedule/sync",
        headers=_bearer(phone),
        json={"events": [_event("slot-1", "classPreparing", fire, "A")]},
    )
    assert phone_again.json() == {"pending": 1, "replaced": 0}
