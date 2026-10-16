"""An assignment reminder leaves out a device that the assignment's
countdown start has just alerted.

The reminder is filed per user and the `assignmentUrgent` start per device,
both from the due time, so when the Live Activity's lead time equals a
reminder offset the two fall due together and the device would ring twice.
Only a start that went out counts: anything else leaves the reminder.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from server.auth.crypto import CredentialCipher
from server.auth.models import (
    DevicePushToken,
    PushDelivery,
    PushJob,
    User,
    UserDevice,
)
from server.db import build_session_factory
from server.push.apns_client import SendResult
from server.push.dedupe import activity_end_key, schedule_key
from server.push.pipeline import PushPipelineWorker, run_push_tick
from server.push.reminders import CHANNEL as REMINDER_CHANNEL
from server.push.reminders import _dedupe_key
from server.push.router import PushRouter
from server.tests.conftest import SubmissionStatusStub

pytestmark = pytest.mark.asyncio(loop_scope="session")


class ScriptedSender:
    """PushSender double that always succeeds. Duplicated rather than
    shared, as in every push test module here."""

    def __init__(self):
        self.requests = []

    async def send(self, request):
        self.requests.append(request)
        return SendResult(success=True, status="200")

    async def send_multi(self, requests):
        return [await self.send(r) for r in requests]

    async def close(self):
        pass


def _worker(prepared_engine, test_settings, apple):
    return PushPipelineWorker(
        session_factory=build_session_factory(prepared_engine),
        settings=test_settings,
        router=PushRouter(apple=apple, android=ScriptedSender()),
        worker_id="countdown-overlap-test-worker",
        # Every assignment Moodle is asked about is open.
        cipher=CredentialCipher.from_settings(test_settings),
        fetcher=SubmissionStatusStub(),
    )


def _ago(minutes):
    return datetime.now(UTC) - timedelta(minutes=minutes)


async def _user(session, store_moodle_token):
    user = User(student_id="b11203058")
    session.add(user)
    await session.flush()
    await store_moodle_token(user.id)
    return user


async def _iphone(session, user, name, *, app_version="2.1.0"):
    """An iPhone that takes reminders and countdown starts."""
    device = UserDevice(
        user_id=user.id,
        client_device_id=f"iphone-{name}",
        platform="ios",
        app_version=app_version,
        cloud_sync_enabled=True,
        sync_assignment_reminders=True,
    )
    session.add(device)
    await session.flush()
    for kind in ("standard", "push_to_start"):
        session.add(
            DevicePushToken(
                device_id=device.id,
                provider="apns",
                token_kind=kind,
                token_hash=f"hash-{kind}-{name}",
                token_value=f"{kind}-{name}",
                bundle_id="org.ntust.app.TigerDuck",
            )
        )
    await session.flush()
    return device


def _reminder(user, assignment_id, due_at, *, fire_at, offset_hours=8.0, attempts=0):
    """A reminder as the scan files it."""
    due_epoch = int(due_at.timestamp())
    return PushJob(
        user_id=user.id,
        dedupe_key=_dedupe_key(assignment_id, offset_hours, due_epoch),
        channel=REMINDER_CHANNEL,
        scenario=f"reminder_{offset_hours:g}h",
        fire_at=fire_at,
        available_at=fire_at,
        attempts=attempts,
        payload={
            "kind": "assignment_reminder",
            "assignment_title": f"HW{assignment_id}",
            "course_name": "資料結構",
            "moodle_assignment_id": assignment_id,
            "moodle_course_id": 7001,
            "due_at": due_at.isoformat(),
            "due_epoch": due_epoch,
            "offset_hours": offset_hours,
        },
    )


def _start(user, device, assignment_id, due_at, *, fire_at, status="pending"):
    """A countdown start as `/schedule/sync` files it."""
    source_id = str(assignment_id)
    return PushJob(
        user_id=user.id,
        device_id=device.id,
        dedupe_key=schedule_key(device.id, source_id, "assignmentUrgent"),
        channel="schedule",
        scenario="assignmentUrgent",
        priority=10,
        fire_at=fire_at,
        available_at=fire_at,
        status=status,
        payload={
            "kind": "schedule",
            "scenario": "assignmentUrgent",
            "source_id": source_id,
            "activity_id": f"assignmentUrgent::{source_id}",
            "title": f"HW{assignment_id}",
            "subtitle": "資料結構",
            "accentHex": 1,
            "sourceId": source_id,
            "countdownTarget": due_at.isoformat(),
        },
    )


async def _handled_start(
    session, user, device, assignment_id, due_at, *, at, delivery="sent"
):
    """A countdown start the pipeline already handled at `at`, its one
    delivery to the device's push-to-start token left as `delivery`."""
    job = _start(
        user,
        device,
        assignment_id,
        due_at,
        fire_at=at,
        status="sent" if delivery == "sent" else "failed",
    )
    session.add(job)
    await session.flush()
    session.add(
        PushDelivery(
            push_job_id=job.id,
            user_id=user.id,
            device_id=device.id,
            provider="apns",
            token_kind="push_to_start",
            token_hash=f"hash-push_to_start-{device.client_device_id}",
            status=delivery,
            sent_at=at if delivery == "sent" else None,
            next_retry_at=at,
        )
    )
    await session.flush()
    return job


async def _standard_token(session, device):
    return (
        await session.execute(
            select(DevicePushToken).where(
                DevicePushToken.device_id == device.id,
                DevicePushToken.token_kind == "standard",
            )
        )
    ).scalar_one()


async def _pending_delivery(session, job, device):
    """A delivery an earlier round of `job` made and has yet to send."""
    token = await _standard_token(session, device)
    session.add(
        PushDelivery(
            push_job_id=job.id,
            user_id=job.user_id,
            device_id=device.id,
            push_token_id=token.id,
            provider="apns",
            token_kind="standard",
            token_hash=token.token_hash,
            attempts=1,
            next_retry_at=_ago(1),
        )
    )
    await session.flush()


async def _reminder_tokens(session, job):
    """The device tokens `job` was delivered to."""
    deliveries = (
        (
            await session.execute(
                select(PushDelivery).where(PushDelivery.push_job_id == job.id)
            )
        )
        .scalars()
        .all()
    )
    return {(d.token_hash, d.status) for d in deliveries}


@pytest.mark.parametrize("offset_hours", [8.0, 2.0])
async def test_a_reminder_due_with_the_countdown_start_is_left_out(
    db_session,
    prepared_engine,
    test_settings,
    make_assignment,
    store_moodle_token,
    offset_hours,
):
    """Whichever offset the lead time equals: the start goes first, and the
    reminder that falls due with it finds every device already alerted."""
    user = await _user(db_session, store_moodle_token)
    iphone = await _iphone(db_session, user, "a")
    fire_at = _ago(1)
    due_at = fire_at + timedelta(hours=offset_hours)
    await make_assignment(user_id=user.id, moodle_assignment_id=61, due_at=due_at)
    start = _start(user, iphone, 61, due_at, fire_at=fire_at)
    reminder = _reminder(
        user, 61, due_at, fire_at=fire_at, offset_hours=offset_hours
    )
    db_session.add_all([start, reminder])
    await db_session.commit()

    apple = ScriptedSender()
    await run_push_tick(_worker(prepared_engine, test_settings, apple))

    await db_session.refresh(start)
    await db_session.refresh(reminder)
    assert start.status == "sent"
    assert [r.device_token for r in apple.requests] == ["push_to_start-a"]
    assert (reminder.status, reminder.last_error) == (
        "cancelled",
        "countdown_start_sent",
    )
    assert reminder.cancelled_at is not None
    assert await _reminder_tokens(db_session, reminder) == set()


async def test_only_the_device_the_start_went_to_is_left_out(
    db_session, prepared_engine, test_settings, make_assignment, store_moodle_token
):
    """The other iPhone has the app's Live Activity switch off, so no start
    was filed for it."""
    user = await _user(db_session, store_moodle_token)
    alerted = await _iphone(db_session, user, "a")
    await _iphone(db_session, user, "b")
    fire_at = _ago(1)
    due_at = fire_at + timedelta(hours=8)
    await make_assignment(user_id=user.id, moodle_assignment_id=62, due_at=due_at)
    await _handled_start(db_session, user, alerted, 62, due_at, at=fire_at)
    reminder = _reminder(user, 62, due_at, fire_at=fire_at)
    db_session.add(reminder)
    await db_session.commit()

    apple = ScriptedSender()
    await run_push_tick(_worker(prepared_engine, test_settings, apple))

    await db_session.refresh(reminder)
    assert reminder.status == "sent"
    assert [r.device_token for r in apple.requests] == ["standard-b"]
    assert await _reminder_tokens(db_session, reminder) == {
        ("hash-standard-b", "sent")
    }


@pytest.mark.parametrize("delivery", ["failed", "pending", "skipped", None])
async def test_a_start_that_did_not_go_out_leaves_the_reminder(
    db_session,
    prepared_engine,
    test_settings,
    make_assignment,
    store_moodle_token,
    delivery,
):
    """Failed, still retrying, skipped, or cancelled before it was sent."""
    user = await _user(db_session, store_moodle_token)
    iphone = await _iphone(db_session, user, "a")
    fire_at = _ago(1)
    due_at = fire_at + timedelta(hours=8)
    await make_assignment(user_id=user.id, moodle_assignment_id=63, due_at=due_at)
    if delivery is None:
        db_session.add(
            _start(user, iphone, 63, due_at, fire_at=fire_at, status="cancelled")
        )
    else:
        start = await _handled_start(
            db_session, user, iphone, 63, due_at, at=fire_at, delivery=delivery
        )
        if delivery == "pending":
            # Waiting out the retry delay, so this tick leaves it alone.
            start.status = "pending"
            start.available_at = datetime.now(UTC) + timedelta(minutes=1)
    reminder = _reminder(user, 63, due_at, fire_at=fire_at)
    db_session.add(reminder)
    await db_session.commit()

    apple = ScriptedSender()
    await run_push_tick(_worker(prepared_engine, test_settings, apple))

    await db_session.refresh(reminder)
    assert reminder.status == "sent"
    assert [r.device_token for r in apple.requests] == ["standard-a"]


@pytest.mark.parametrize(
    "minutes_ahead, left_out", [(4, True), (6, False), (30, False)]
)
async def test_only_a_start_sent_within_minutes_of_the_reminder_counts(
    db_session,
    prepared_engine,
    test_settings,
    make_assignment,
    store_moodle_token,
    minutes_ahead,
    left_out,
):
    """Up to five minutes ahead of the reminder's fire time. A start sent
    earlier was for another moment, such as a lead time half an hour off the
    offset."""
    user = await _user(db_session, store_moodle_token)
    iphone = await _iphone(db_session, user, "a")
    fire_at = _ago(1)
    due_at = fire_at + timedelta(hours=8)
    await make_assignment(user_id=user.id, moodle_assignment_id=64, due_at=due_at)
    sent_at = fire_at - timedelta(minutes=minutes_ahead)
    await _handled_start(db_session, user, iphone, 64, due_at, at=sent_at)
    reminder = _reminder(user, 64, due_at, fire_at=fire_at)
    db_session.add(reminder)
    await db_session.commit()

    apple = ScriptedSender()
    await run_push_tick(_worker(prepared_engine, test_settings, apple))

    await db_session.refresh(reminder)
    if left_out:
        assert (reminder.status, reminder.last_error) == (
            "cancelled",
            "countdown_start_sent",
        )
        assert apple.requests == []
    else:
        assert reminder.status == "sent"
        assert [r.device_token for r in apple.requests] == ["standard-a"]


async def test_a_start_for_another_assignment_leaves_the_reminder(
    db_session, prepared_engine, test_settings, make_assignment, store_moodle_token
):
    user = await _user(db_session, store_moodle_token)
    iphone = await _iphone(db_session, user, "a")
    fire_at = _ago(1)
    due_at = fire_at + timedelta(hours=8)
    await make_assignment(user_id=user.id, moodle_assignment_id=65, due_at=due_at)
    await _handled_start(db_session, user, iphone, 66, due_at, at=fire_at)
    reminder = _reminder(user, 65, due_at, fire_at=fire_at)
    db_session.add(reminder)
    await db_session.commit()

    apple = ScriptedSender()
    await run_push_tick(_worker(prepared_engine, test_settings, apple))

    await db_session.refresh(reminder)
    assert reminder.status == "sent"
    assert [r.device_token for r in apple.requests] == ["standard-a"]


async def test_a_later_round_still_leaves_the_device_out(
    db_session, prepared_engine, test_settings, make_assignment, store_moodle_token
):
    """The window counts from the reminder's fire time, not the clock: a
    round minutes later must not hand the alerted device the reminder."""
    user = await _user(db_session, store_moodle_token)
    alerted = await _iphone(db_session, user, "a")
    other = await _iphone(db_session, user, "b")
    fire_at = _ago(8)
    due_at = fire_at + timedelta(hours=8)
    await make_assignment(user_id=user.id, moodle_assignment_id=67, due_at=due_at)
    await _handled_start(
        db_session, user, alerted, 67, due_at, at=fire_at + timedelta(minutes=1)
    )
    reminder = _reminder(user, 67, due_at, fire_at=fire_at, attempts=1)
    db_session.add(reminder)
    await db_session.flush()
    await _pending_delivery(db_session, reminder, other)
    await db_session.commit()

    apple = ScriptedSender()
    await run_push_tick(_worker(prepared_engine, test_settings, apple))

    await db_session.refresh(reminder)
    assert reminder.status == "sent"
    assert [r.device_token for r in apple.requests] == ["standard-b"]
    assert await _reminder_tokens(db_session, reminder) == {
        ("hash-standard-b", "sent")
    }


async def test_a_delivery_made_before_the_start_went_out_is_still_sent(
    db_session, prepared_engine, test_settings, make_assignment, store_moodle_token
):
    """An earlier round gave the device the reminder while its start was
    still retrying. That delivery is finished, not cancelled under it."""
    user = await _user(db_session, store_moodle_token)
    iphone = await _iphone(db_session, user, "a")
    fire_at = _ago(3)
    due_at = fire_at + timedelta(hours=8)
    await make_assignment(user_id=user.id, moodle_assignment_id=68, due_at=due_at)
    await _handled_start(
        db_session, user, iphone, 68, due_at, at=fire_at + timedelta(minutes=1)
    )
    reminder = _reminder(user, 68, due_at, fire_at=fire_at, attempts=1)
    db_session.add(reminder)
    await db_session.flush()
    await _pending_delivery(db_session, reminder, iphone)
    await db_session.commit()

    apple = ScriptedSender()
    await run_push_tick(_worker(prepared_engine, test_settings, apple))

    await db_session.refresh(reminder)
    assert reminder.status == "sent"
    assert [r.device_token for r in apple.requests] == ["standard-a"]


async def test_an_end_filed_right_after_the_start_still_goes(
    db_session, prepared_engine, test_settings, store_moodle_token
):
    """A submission minutes after the start ends the countdown at once
    (`submission_cancel`); only reminders are left out."""
    user = await _user(db_session, store_moodle_token)
    iphone = await _iphone(db_session, user, "a")
    activity_id = "assignmentUrgent::70"
    db_session.add(
        DevicePushToken(
            device_id=iphone.id,
            provider="apns",
            token_kind="live_activity_update",
            token_hash="hash-la-70",
            token_value="la-70",
            bundle_id="org.ntust.app.TigerDuck",
            scope_key=activity_id,
        )
    )
    await _handled_start(
        db_session, user, iphone, 70, datetime.now(UTC) + timedelta(hours=8), at=_ago(1)
    )
    end = PushJob(
        user_id=user.id,
        device_id=iphone.id,
        dedupe_key=activity_end_key(iphone.id, activity_id),
        channel="schedule",
        scenario="activityEnd",
        priority=10,
        fire_at=_ago(0),
        available_at=_ago(0),
        payload={
            "kind": "live_activity_end",
            "activity_id": activity_id,
            "source_id": "70",
        },
    )
    db_session.add(end)
    await db_session.commit()

    apple = ScriptedSender()
    await run_push_tick(_worker(prepared_engine, test_settings, apple))

    await db_session.refresh(end)
    assert end.status == "sent"
    assert [r.device_token for r in apple.requests] == ["la-70"]


async def test_a_reminder_no_device_takes_for_another_reason_still_fails(
    db_session, prepared_engine, test_settings, make_assignment, store_moodle_token
):
    """An app that arms its own reminders takes none from the server; its
    start does not make the reminder look delivered."""
    user = await _user(db_session, store_moodle_token)
    iphone = await _iphone(db_session, user, "a", app_version="2.0.2")
    fire_at = _ago(1)
    due_at = fire_at + timedelta(hours=8)
    await make_assignment(user_id=user.id, moodle_assignment_id=69, due_at=due_at)
    await _handled_start(db_session, user, iphone, 69, due_at, at=fire_at)
    reminder = _reminder(user, 69, due_at, fire_at=fire_at)
    db_session.add(reminder)
    await db_session.commit()

    apple = ScriptedSender()
    await run_push_tick(_worker(prepared_engine, test_settings, apple))

    await db_session.refresh(reminder)
    assert (reminder.status, reminder.last_error) == ("failed", "no_active_tokens")
    assert apple.requests == []
