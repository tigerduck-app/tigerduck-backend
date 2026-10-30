"""The sync trigger's pace: a background push asks every device of the user
to sync, and Apple advises two or three of those an hour at most (#107)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from server.auth.crypto import CredentialCipher
from server.auth.models import DevicePushToken, PushDelivery, PushJob, User, UserDevice
from server.db import build_session_factory
from server.push.apns_client import SendResult
from server.push.pipeline import PushPipelineWorker, run_push_tick
from server.push.router import PushRouter
from server.push.sync_trigger import MIN_GAP, enqueue_sync_trigger
from server.tests.conftest import SubmissionStatusStub

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _user(session):
    user = User(student_id="b11203058")
    session.add(user)
    await session.commit()
    return user


async def _enqueue(prepared_engine, user, source_device_id=None):
    """One writer's transaction, committed."""
    async with build_session_factory(prepared_engine)() as session:
        job_id = await enqueue_sync_trigger(
            session, user.id, source_device_id=source_device_id
        )
        await session.commit()
    return job_id


async def _triggers(session, user):
    return (
        (
            await session.execute(
                select(PushJob)
                .where(PushJob.user_id == user.id, PushJob.scenario == "sync_trigger")
                .order_by(PushJob.fire_at)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )


async def test_changes_while_a_trigger_waits_ride_on_it(db_session, prepared_engine):
    user = await _user(db_session)

    first = await _enqueue(prepared_engine, user, "iphone")
    second = await _enqueue(prepared_engine, user, "ipad")

    assert first is not None and second is None
    [trigger] = await _triggers(db_session, user)
    # Each device made a change the other has to fetch.
    assert trigger.payload["source_device_id"] is None


async def test_a_change_after_a_trigger_went_out_waits_out_the_gap(
    db_session, prepared_engine
):
    """The old five-minute key let one through every five minutes, twelve an
    hour to every device."""
    user = await _user(db_session)
    await _enqueue(prepared_engine, user)
    [sent] = await _triggers(db_session, user)
    sent.status = "sent"
    await db_session.commit()

    await _enqueue(prepared_engine, user)
    await _enqueue(prepared_engine, user)

    triggers = await _triggers(db_session, user)
    assert [t.status for t in triggers] == ["sent", "pending"]
    assert triggers[1].fire_at == sent.fire_at + MIN_GAP


@pytest.mark.parametrize("going_out", ["claimed", "retrying"])
async def test_a_change_never_rides_on_a_trigger_going_out(
    db_session, prepared_engine, going_out
):
    """A claimed trigger may reach a device before this change commits, and
    one retrying has reached some already."""
    user = await _user(db_session)
    await _enqueue(prepared_engine, user)
    [first] = await _triggers(db_session, user)
    factory = build_session_factory(prepared_engine)
    async with factory() as claim:
        if going_out == "claimed":
            await claim.execute(
                select(PushJob).where(PushJob.id == first.id).with_for_update()
            )
        else:
            claim.add(
                PushDelivery(
                    push_job_id=first.id,
                    user_id=user.id,
                    provider="apns",
                    token_kind="standard",
                    token_hash="hash",
                    status="sent",
                    sent_at=datetime.now(UTC),
                )
            )
            await claim.commit()

        await _enqueue(prepared_engine, user)

    triggers = await _triggers(db_session, user)
    assert len(triggers) == 2
    assert triggers[1].fire_at == first.fire_at + MIN_GAP


async def test_the_first_trigger_goes_at_once(db_session, prepared_engine):
    user = await _user(db_session)
    before = datetime.now(UTC)

    await _enqueue(prepared_engine, user)

    [trigger] = await _triggers(db_session, user)
    assert before <= trigger.fire_at <= datetime.now(UTC) + timedelta(seconds=1)


async def test_a_writer_that_loses_the_insert_rides_on_the_winners_trigger(
    db_session, prepared_engine
):
    """Two writers read the same latest trigger and file the next one under
    the same key. The loser used to drop its change; the winner's trigger
    could then go out before the loser committed it."""
    user = await _user(db_session)
    await _enqueue(prepared_engine, user)
    [sent] = await _triggers(db_session, user)
    sent.status = "sent"
    await db_session.commit()
    factory = build_session_factory(prepared_engine)

    async with factory() as winner, factory() as loser:
        await enqueue_sync_trigger(winner, user.id, source_device_id="iphone")
        losing = asyncio.create_task(
            enqueue_sync_trigger(loser, user.id, source_device_id="ipad")
        )
        # The loser's insert waits on the winner's uncommitted row.
        for _ in range(500):
            waiting = await db_session.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE wait_event_type = 'Lock' AND datname = current_database()"
                )
            )
            if waiting:
                break
            await asyncio.sleep(0.01)
        await winner.commit()
        assert await losing is None
        await loser.commit()

    triggers = await _triggers(db_session, user)
    assert len(triggers) == 2
    # Both devices' changes ride on it, so neither device is left out.
    assert triggers[1].payload["source_device_id"] is None


class _Sender:
    def __init__(self):
        self.requests = []

    async def send(self, request):
        self.requests.append(request)
        return SendResult(success=True, status="200")

    async def send_multi(self, requests):
        return [await self.send(r) for r in requests]

    async def close(self):
        pass


async def _tick(prepared_engine, test_settings):
    """One push tick; returns the pushes it sent."""
    apple = _Sender()
    await run_push_tick(
        PushPipelineWorker(
            session_factory=build_session_factory(prepared_engine),
            settings=test_settings,
            router=PushRouter(apple=apple, android=_Sender()),
            worker_id="sync-trigger-test",
            cipher=CredentialCipher.from_settings(test_settings),
            fetcher=SubmissionStatusStub(),
        )
    )
    return apple.requests


async def _iphone(session, user):
    device = UserDevice(user_id=user.id, client_device_id="iphone", platform="ios")
    session.add(device)
    await session.flush()
    session.add(
        DevicePushToken(
            device_id=device.id,
            provider="apns",
            token_kind="standard",
            token_hash="hash-iphone",
            token_value="tok-iphone",
            bundle_id="org.ntust.app.TigerDuck",
        )
    )
    await session.commit()


async def test_a_trigger_sent_late_spaces_the_next_from_its_send(
    db_session, prepared_engine, test_settings
):
    """A trigger due 25 minutes ago went out only now, after downtime. Its
    planned time would let the next go at once."""
    user = await _user(db_session)
    await _iphone(db_session, user)
    await _enqueue(prepared_engine, user)
    assert len(await _tick(prepared_engine, test_settings)) == 1
    [sent] = await _triggers(db_session, user)
    sent.fire_at -= timedelta(minutes=25)
    await db_session.commit()
    sent_at = await db_session.scalar(
        select(PushDelivery.sent_at).where(PushDelivery.push_job_id == sent.id)
    )

    await _enqueue(prepared_engine, user)

    assert await _tick(prepared_engine, test_settings) == []
    [_, paced] = await _triggers(db_session, user)
    assert paced.status == "pending"
    assert paced.available_at == sent_at + MIN_GAP


async def test_overdue_triggers_go_out_one_per_gap(
    db_session, prepared_engine, test_settings
):
    user = await _user(db_session)
    await _iphone(db_session, user)
    due = datetime.now(UTC) - timedelta(hours=1)
    db_session.add_all(
        PushJob(
            user_id=user.id,
            dedupe_key=f"sync_trigger:{user.id}:after:{n}",
            channel="system",
            scenario="sync_trigger",
            fire_at=due + n * MIN_GAP,
            available_at=due + n * MIN_GAP,
            payload={"kind": "sync_trigger", "source_device_id": None},
        )
        for n in range(2)
    )
    await db_session.commit()

    assert len(await _tick(prepared_engine, test_settings)) == 1
    assert sorted(t.status for t in await _triggers(db_session, user)) == [
        "pending",
        "sent",
    ]


async def test_the_portals_forced_trigger_skips_the_gap(
    db_session, prepared_engine, test_settings
):
    """An operator presses the force button to sync now, and the portal
    reports success at once."""
    user = await _user(db_session)
    await _iphone(db_session, user)
    await _enqueue(prepared_engine, user)
    assert len(await _tick(prepared_engine, test_settings)) == 1
    # As portal/app/routes/moodle/push.py files it.
    forced = PushJob(
        user_id=user.id,
        dedupe_key=f"sync_trigger:{user.id}:forced",
        channel="system",
        scenario="sync_trigger",
        fire_at=datetime.now(UTC) - timedelta(seconds=1),
        available_at=datetime.now(UTC) - timedelta(seconds=1),
        payload={"kind": "sync_trigger", "source": "portal_force"},
    )
    db_session.add(forced)
    await db_session.commit()

    assert len(await _tick(prepared_engine, test_settings)) == 1
    await db_session.refresh(forced)
    assert forced.status == "sent"
