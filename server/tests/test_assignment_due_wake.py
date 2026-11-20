"""The sync trigger at an assignment reminder's due time.

iOS cannot expire a delivered notification by itself: the app has to run to
remove a reminder whose assignment is due. So once the due time passes, the
devices of a user who was sent a reminder for it get one background push,
the existing sync trigger, which wakes the app.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from server.auth.crypto import CredentialCipher
from server.auth.models import DevicePushToken, PushDelivery, PushJob, User, UserDevice
from server.db import build_session_factory
from server.push.apns_client import RecordingSender, SendResult
from server.push.fcm_client import RecordingFcmSender
from server.push.pipeline import PushPipelineWorker, run_push_tick
from server.push.reminders import CHANNEL as REMINDER_CHANNEL
from server.push.reminders import wake_at_due_times
from server.push.router import PushRouter
from server.push.sync_trigger import MIN_GAP
from server.scheduler.runtime import build_scheduler
from server.sync.models import UserAssignment
from server.tests.conftest import SubmissionStatusStub

asyncio_session = pytest.mark.asyncio(loop_scope="session")


async def _user(session, name="b11203058"):
    user = User(student_id=name)
    session.add(user)
    await session.flush()
    device = UserDevice(
        user_id=user.id,
        client_device_id=f"iphone-{name}",
        platform="ios",
        app_version="4.0.0",
    )
    session.add(device)
    await session.flush()
    session.add(
        DevicePushToken(
            device_id=device.id,
            provider="apns",
            token_kind="standard",
            token_hash=f"hash-{name}",
            token_value=f"tok-{name}",
            bundle_id="org.ntust.app.TigerDuck",
        )
    )
    await session.flush()
    return user


async def _reminder(
    session,
    user,
    *,
    due_at,
    assignment_id=3,
    offset_hours=24.0,
    status="sent",
    delivery="sent",
):
    """A reminder as the pipeline leaves it, with one delivery in `delivery`
    (none for None)."""
    due_epoch = int(due_at.timestamp())
    job = PushJob(
        user_id=user.id,
        dedupe_key=f"assignment:moodle:{assignment_id}:reminder_{offset_hours:g}h:{due_epoch}",
        channel=REMINDER_CHANNEL,
        scenario=f"reminder_{offset_hours:g}h",
        fire_at=due_at - timedelta(hours=offset_hours),
        status=status,
        payload={
            "kind": "assignment_reminder",
            "assignment_title": "HW3",
            "course_name": "資料結構",
            "moodle_assignment_id": assignment_id,
            "moodle_course_id": 7001,
            "due_at": due_at.isoformat(),
            "due_epoch": due_epoch,
            "offset_hours": offset_hours,
        },
    )
    session.add(job)
    await session.flush()
    if delivery is not None:
        session.add(
            PushDelivery(
                push_job_id=job.id,
                user_id=user.id,
                provider="apns",
                token_kind="standard",
                token_hash=f"hash-{job.id}",
                status=delivery,
                sent_at=job.fire_at if delivery == "sent" else None,
            )
        )
        await session.flush()
    return job


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


def _ago(**kwargs) -> datetime:
    return datetime.now(UTC) - timedelta(**kwargs)


@asyncio_session
async def test_one_wake_per_user_and_due_time(db_session, prepared_engine):
    """Two assignments due at 23:59, two reminders each: one trigger."""
    due_at = _ago(seconds=10)
    user = await _user(db_session)
    other = await _user(db_session, "b11203059")
    for assignment_id in (3, 4):
        for offset in (24.0, 2.0):
            await _reminder(
                db_session, user, due_at=due_at, assignment_id=assignment_id, offset_hours=offset
            )
    await _reminder(db_session, other, due_at=due_at)
    await db_session.commit()
    before = datetime.now(UTC)

    woken = await wake_at_due_times(build_session_factory(prepared_engine))

    assert woken == 2
    for someone in (user, other):
        [trigger] = await _triggers(db_session, someone)
        assert trigger.status == "pending"
        # No trigger went out in the last 20 minutes, so it goes at once.
        assert before <= trigger.fire_at <= datetime.now(UTC)


@asyncio_session
async def test_two_due_times_in_one_pass_share_one_trigger(db_session, prepared_engine):
    """The second rides on the trigger the first filed in the same pass."""
    user = await _user(db_session)
    await _reminder(db_session, user, due_at=_ago(seconds=10), assignment_id=3)
    await _reminder(db_session, user, due_at=_ago(minutes=5), assignment_id=4)
    await db_session.commit()

    assert await wake_at_due_times(build_session_factory(prepared_engine)) == 2
    assert len(await _triggers(db_session, user)) == 1


@asyncio_session
async def test_a_wake_goes_out_even_if_the_assignment_was_submitted_since(
    db_session, prepared_engine
):
    """The reminder may still be on screen."""
    due_at = _ago(seconds=10)
    user = await _user(db_session)
    db_session.add(
        UserAssignment(
            user_id=user.id,
            moodle_course_id=7001,
            moodle_assignment_id=3,
            title="HW3",
            due_at=due_at,
            provider_is_submitted=True,
        )
    )
    await _reminder(db_session, user, due_at=due_at)
    await db_session.commit()

    await wake_at_due_times(build_session_factory(prepared_engine))

    assert len(await _triggers(db_session, user)) == 1


@asyncio_session
@pytest.mark.parametrize(
    ("status", "delivery"),
    [
        ("failed", "failed"),
        ("failed", None),
        ("cancelled", None),
        ("pending", None),
        ("failed", "skipped"),
    ],
)
async def test_no_wake_without_a_sent_reminder(
    db_session, prepared_engine, status, delivery
):
    user = await _user(db_session)
    await _reminder(
        db_session, user, due_at=_ago(seconds=10), status=status, delivery=delivery
    )
    await db_session.commit()

    assert await wake_at_due_times(build_session_factory(prepared_engine)) == 0
    assert await _triggers(db_session, user) == []


@asyncio_session
@pytest.mark.parametrize("due_epoch", ["word", "number-as-text", "missing"])
async def test_a_reminder_without_a_numeric_due_time_wakes_nothing(
    db_session, prepared_engine, due_epoch
):
    """The scan writes the due time as a JSON number; anything else is left
    alone rather than read, or cast and failing the pass."""
    user = await _user(db_session)
    job = await _reminder(db_session, user, due_at=_ago(seconds=10))
    payload = dict(job.payload)
    if due_epoch == "missing":
        del payload["due_epoch"]
    elif due_epoch == "word":
        payload["due_epoch"] = "soon"
    else:
        payload["due_epoch"] = str(payload["due_epoch"])
    job.payload = payload
    await db_session.commit()

    assert await wake_at_due_times(build_session_factory(prepared_engine)) == 0
    assert await _triggers(db_session, user) == []


@asyncio_session
async def test_no_trigger_for_a_user_with_no_device_to_take_it(
    db_session, prepared_engine, test_settings
):
    """With TigerSync turned off since the reminder, a trigger would only
    settle as failed (`no_active_tokens`), one row per due time."""
    user = await _user(db_session)
    await _reminder(db_session, user, due_at=_ago(seconds=10))
    device = (
        await db_session.execute(select(UserDevice).where(UserDevice.user_id == user.id))
    ).scalar_one()
    device.cloud_sync_enabled = False
    await db_session.commit()

    assert await wake_at_due_times(build_session_factory(prepared_engine)) == 0
    assert await _triggers(db_session, user) == []


@asyncio_session
async def test_a_partly_sent_reminder_counts(db_session, prepared_engine):
    user = await _user(db_session)
    await _reminder(db_session, user, due_at=_ago(seconds=10), status="partial_failed")
    await db_session.commit()

    await wake_at_due_times(build_session_factory(prepared_engine))

    assert len(await _triggers(db_session, user)) == 1


@asyncio_session
async def test_no_second_wake_after_a_rerun(db_session, prepared_engine):
    due_at = _ago(seconds=10)
    user = await _user(db_session)
    await _reminder(db_session, user, due_at=due_at)
    await db_session.commit()
    factory = build_session_factory(prepared_engine)

    await wake_at_due_times(factory)
    [trigger] = await _triggers(db_session, user)
    # The trigger went out, so another would be filed rather than ride.
    trigger.status = "sent"
    # A reminder of the same due time that settled after the wake.
    await _reminder(db_session, user, due_at=due_at, assignment_id=4)
    await db_session.commit()

    assert await wake_at_due_times(factory) == 0
    assert [t.id for t in await _triggers(db_session, user)] == [trigger.id]


@asyncio_session
async def test_overlapping_runs_wake_once(db_session, prepared_engine):
    user = await _user(db_session)
    await _reminder(db_session, user, due_at=_ago(seconds=10))
    await db_session.commit()
    factory = build_session_factory(prepared_engine)

    woken = await asyncio.gather(wake_at_due_times(factory), wake_at_due_times(factory))

    assert sorted(woken) == [0, 1]
    assert len(await _triggers(db_session, user)) == 1


@asyncio_session
async def test_no_trigger_is_filed_ahead_of_the_due_time(db_session, prepared_engine):
    """A trigger that has yet to go out takes every later change as a ride, so
    one filed for a due time ahead would hold back every sync until then."""
    user = await _user(db_session)
    job = await _reminder(
        db_session, user, due_at=datetime.now(UTC) + timedelta(minutes=2)
    )
    await db_session.commit()
    factory = build_session_factory(prepared_engine)

    assert await wake_at_due_times(factory) == 0
    assert await _triggers(db_session, user) == []

    # The due time passes.
    job.payload = {**job.payload, "due_epoch": int(_ago(seconds=1).timestamp())}
    await db_session.commit()
    await wake_at_due_times(factory)
    [trigger] = await _triggers(db_session, user)
    assert trigger.fire_at <= datetime.now(UTC)


@asyncio_session
@pytest.mark.parametrize(("missed_by", "woken"), [(59, 1), (61, 0)])
async def test_a_due_time_missed_by_over_an_hour_is_skipped(
    db_session, prepared_engine, missed_by, woken
):
    """After downtime the app clears the reminder when it next comes to the
    foreground."""
    user = await _user(db_session)
    await _reminder(db_session, user, due_at=_ago(minutes=missed_by))
    await db_session.commit()

    assert await wake_at_due_times(build_session_factory(prepared_engine)) == woken
    assert len(await _triggers(db_session, user)) == woken


@asyncio_session
async def test_the_wake_keeps_twenty_minutes_after_the_last_trigger(
    db_session, prepared_engine
):
    user = await _user(db_session)
    last = PushJob(
        user_id=user.id,
        dedupe_key=f"sync_trigger:{user.id}:after:0",
        channel="system",
        scenario="sync_trigger",
        fire_at=_ago(minutes=5),
        status="sent",
        payload={"kind": "sync_trigger", "source_device_id": None},
    )
    db_session.add(last)
    await _reminder(db_session, user, due_at=_ago(seconds=10))
    await db_session.commit()

    await wake_at_due_times(build_session_factory(prepared_engine))

    [_, wake] = await _triggers(db_session, user)
    assert wake.status == "pending"
    assert wake.fire_at == last.fire_at + MIN_GAP


@asyncio_session
async def test_the_wake_rides_on_a_trigger_yet_to_go_out(db_session, prepared_engine):
    user = await _user(db_session)
    waiting = PushJob(
        user_id=user.id,
        dedupe_key=f"sync_trigger:{user.id}:after:0",
        channel="system",
        scenario="sync_trigger",
        fire_at=datetime.now(UTC) + timedelta(minutes=10),
        payload={"kind": "sync_trigger", "source_device_id": "iphone-b11203058"},
    )
    db_session.add(waiting)
    await _reminder(db_session, user, due_at=_ago(seconds=10))
    await db_session.commit()

    await wake_at_due_times(build_session_factory(prepared_engine))

    [trigger] = await _triggers(db_session, user)
    assert trigger.id == waiting.id
    # The device that made the change has a reminder to clear too.
    assert trigger.payload["source_device_id"] is None


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


@asyncio_session
async def test_the_wake_reaches_the_device_as_a_background_push(
    db_session, prepared_engine, test_settings
):
    user = await _user(db_session)
    await _reminder(db_session, user, due_at=_ago(seconds=10))
    await db_session.commit()
    factory = build_session_factory(prepared_engine)

    await wake_at_due_times(factory)
    apple = _Sender()
    await run_push_tick(
        PushPipelineWorker(
            session_factory=factory,
            settings=test_settings,
            router=PushRouter(apple=apple, android=_Sender()),
            worker_id="due-wake-test",
            cipher=CredentialCipher.from_settings(test_settings),
            fetcher=SubmissionStatusStub(),
        )
    )

    [request] = apple.requests
    assert request.device_token == "tok-b11203058"
    assert request.message == {"aps": {"content-available": 1}, "kind": "sync_trigger"}


def _scheduled_wake(settings):
    router = PushRouter(apple=RecordingSender(), android=RecordingFcmSender())
    scheduler = build_scheduler(
        async_sessionmaker(),
        router,
        settings,
        llm=object(),
        push_worker=PushPipelineWorker(
            session_factory=async_sessionmaker(),
            settings=settings,
            router=router,
            worker_id="w",
        ),
    )
    return scheduler.get_job("assignment_due_wake")


def test_the_wake_runs_with_the_push_pipeline_every_half_minute(test_settings):
    assert test_settings.assignment_due_wake_interval_seconds == 30
    job = _scheduled_wake(test_settings)
    assert job is not None
    assert job.trigger.interval == timedelta(seconds=30)


def test_an_interval_of_zero_turns_the_wake_off(test_settings):
    settings = test_settings.model_copy(
        update={"assignment_due_wake_interval_seconds": 0}
    )
    assert _scheduled_wake(settings) is None
