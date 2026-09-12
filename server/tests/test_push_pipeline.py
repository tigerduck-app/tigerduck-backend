"""Push pipeline: stale recovery, claim, materialize, deliver, aggregate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from server.auth.models import (
    DevicePushToken,
    PushDelivery,
    PushJob,
    User,
    UserDevice,
)
from server.auth.models import push as push_models_module
from server.db import build_session_factory
from server.push import pipeline as pipeline_module
from server.push.apns_client import SendResult
from server.push.dedupe import activity_end_key, schedule_key
from server.push.pipeline import PushPipelineWorker, run_push_tick
from server.push.reminders import CHANNEL as REMINDER_CHANNEL
from server.push.router import PushRouter

pytestmark = pytest.mark.asyncio(loop_scope="session")


class ScriptedSender:
    """PushSender double returning queued results (last one repeats)."""

    def __init__(self, results=None):
        self.results = list(results or [])
        self.requests = []

    async def send(self, request):
        self.requests.append(request)
        if len(self.results) > 1:
            return self.results.pop(0)
        return (
            self.results[0]
            if self.results
            else SendResult(success=True, status="200")
        )

    async def send_multi(self, requests):
        return [await self.send(r) for r in requests]

    async def close(self):
        pass


def _worker(prepared_engine, test_settings, apple=None, android=None):
    return PushPipelineWorker(
        session_factory=build_session_factory(prepared_engine),
        settings=test_settings,
        router=PushRouter(
            apple=apple or ScriptedSender(), android=android or ScriptedSender()
        ),
        worker_id="push-test-worker",
    )


async def _setup_user_device_token(
    session,
    *,
    platform="ios",
    provider="apns",
    token_value="tok-1",
    token_kwargs=None,
):
    user = User(student_id="b11203058")
    session.add(user)
    await session.flush()
    device = UserDevice(
        user_id=user.id, client_device_id="dev-1", platform=platform
    )
    session.add(device)
    await session.flush()
    token = DevicePushToken(
        device_id=device.id,
        provider=provider,
        token_kind="standard",
        token_hash=f"hash-{token_value}",
        token_value=token_value,
        bundle_id="org.ntust.app.TigerDuck",
        **(token_kwargs or {}),
    )
    session.add(token)
    await session.flush()
    return user, device, token


def _job(user, **kwargs):
    defaults = dict(
        user_id=user.id,
        dedupe_key=f"system:test:{kwargs.get('scenario', 's')}",
        channel="system",
        scenario="reauth_required",
        # Explicit, COMFORTABLY past values. The column's server_default
        # now() is the INSERT transaction timestamp (can land after the
        # tick's claim cutoff), and a 1-second margin proved flaky when the
        # host clock stepped backwards mid-run (NTP) — single-tick tests
        # can't self-heal from a missed claim the way the loop tests do.
        fire_at=datetime.now(UTC) - timedelta(minutes=5),
        available_at=datetime.now(UTC) - timedelta(minutes=5),
        payload={"title": "t", "body": "b"},
    )
    defaults.update(kwargs)
    return PushJob(**defaults)


async def test_due_job_claimed_and_sent(
    db_session, prepared_engine, test_settings
):
    user, _, _ = await _setup_user_device_token(db_session)
    job = _job(user)
    db_session.add(job)
    await db_session.commit()

    apple = ScriptedSender([SendResult(success=True, status="200")])
    worker = _worker(prepared_engine, test_settings, apple=apple)
    await run_push_tick(worker)

    assert len(apple.requests) == 1
    await db_session.refresh(job)
    assert job.status == "sent"
    assert job.sent_at is not None
    delivery = (
        await db_session.execute(
            select(PushDelivery).where(PushDelivery.push_job_id == job.id)
        )
    ).scalar_one()
    assert delivery.status == "sent"
    assert delivery.provider == "apns"


@pytest.mark.parametrize(
    "database_clock_lead",
    [timedelta(seconds=-5), timedelta(0), timedelta(seconds=5)],
    ids=["database-behind", "clocks-agree", "database-ahead"],
)
async def test_a_delivery_made_this_tick_goes_out_whichever_way_the_clocks_disagree(
    db_session, prepared_engine, test_settings, monkeypatch, database_clock_lead
):
    """A tick materializes a job's deliveries, then sends each one whose
    `next_retry_at` has come by the pipeline's own clock. A delivery made
    moments earlier in the same tick must qualify. Stamped instead by the
    database's clock, one running even a few milliseconds ahead (a Docker
    VM's clock drifts against its host's) leaves every fresh delivery
    waiting out a retry round while the job spends an attempt sending
    nothing.

    Pinned by running the pipeline on a clock that lags the database's and
    on one that leads it: the push goes out on the first tick either way."""
    user, _, _ = await _setup_user_device_token(db_session)
    job = _job(user)
    db_session.add(job)
    await db_session.commit()

    real_datetime = pipeline_module.datetime

    class SkewedDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime.now(tz) - database_clock_lead

    monkeypatch.setattr(pipeline_module, "datetime", SkewedDatetime)
    apple = ScriptedSender([SendResult(success=True, status="200")])
    await run_push_tick(_worker(prepared_engine, test_settings, apple=apple))

    assert len(apple.requests) == 1
    await db_session.refresh(job)
    assert job.status == "sent"


@pytest.mark.parametrize(
    "database_clock_lead",
    [timedelta(seconds=-5), timedelta(0), timedelta(seconds=5)],
    ids=["database-behind", "clocks-agree", "database-ahead"],
)
async def test_a_job_queued_without_available_at_is_claimed_whichever_way_the_clocks_disagree(
    db_session, prepared_engine, test_settings, monkeypatch, database_clock_lead
):
    """Every `pg_insert(PushJob)` call site in product code omits
    `available_at` and takes the column's default. Left to the database's
    `now()`, a database clock even a few milliseconds ahead of the app
    host's (a Docker VM's drifts against its host's) stamps it later than
    the claim's own `now`, and `_trigger_push_tick` firing immediately
    after the job is queued (as it does from `_enqueue_sync_trigger`) can
    miss the job for a whole retry round.

    Pinned by constructing the job on a clock that lags the claim's and on
    one that leads it: the job is claimed on its very first tick either
    way, because the default and the claim read the same clock."""
    real_datetime = pipeline_module.datetime

    class SkewedDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime.now(tz) - database_clock_lead

    monkeypatch.setattr(pipeline_module, "datetime", SkewedDatetime)
    monkeypatch.setattr(push_models_module, "datetime", SkewedDatetime)

    user, _, _ = await _setup_user_device_token(db_session)
    job = PushJob(
        user_id=user.id,
        dedupe_key="system:test:no-available-at",
        channel="system",
        scenario="reauth_required",
        fire_at=datetime.now(UTC) - timedelta(minutes=5),
        payload={"title": "t", "body": "b"},
        # available_at deliberately omitted -- exercises the column default.
    )
    db_session.add(job)
    await db_session.commit()

    apple = ScriptedSender([SendResult(success=True, status="200")])
    await run_push_tick(_worker(prepared_engine, test_settings, apple=apple))

    assert len(apple.requests) == 1


async def test_future_or_unavailable_jobs_not_claimed(
    db_session, prepared_engine, test_settings
):
    user, _, _ = await _setup_user_device_token(db_session)
    db_session.add(
        _job(user, fire_at=datetime.now(UTC) + timedelta(hours=1), scenario="f")
    )
    db_session.add(
        _job(
            user,
            available_at=datetime.now(UTC) + timedelta(minutes=5),
            scenario="a",
            dedupe_key="system:test:a2",
        )
    )
    await db_session.commit()

    apple = ScriptedSender()
    worker = _worker(prepared_engine, test_settings, apple=apple)
    await run_push_tick(worker)
    assert apple.requests == []


async def test_stale_processing_job_recovered_with_backoff(
    db_session, prepared_engine, test_settings
):
    user, _, _ = await _setup_user_device_token(db_session)
    job = _job(
        user,
        status="processing",
        locked_by="dead",
        locked_at=datetime.now(UTC) - timedelta(minutes=6),
        available_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    db_session.add(job)
    await db_session.commit()

    worker = _worker(prepared_engine, test_settings)
    await run_push_tick(worker)

    await db_session.refresh(job)
    # attempts++ and pushed back to pending with future available_at...
    # then NOT claimed this tick (available_at in the future).
    assert job.attempts == 1
    assert job.status == "pending"
    assert job.available_at > datetime.now(UTC)


async def test_stale_processing_exhausted_goes_failed(
    db_session, prepared_engine, test_settings
):
    user, _, _ = await _setup_user_device_token(db_session)
    job = _job(
        user,
        status="processing",
        locked_by="dead",
        locked_at=datetime.now(UTC) - timedelta(minutes=6),
        attempts=2,
        max_attempts=3,
    )
    db_session.add(job)
    await db_session.commit()

    worker = _worker(prepared_engine, test_settings)
    await run_push_tick(worker)
    await db_session.refresh(job)
    assert job.status == "failed"


async def test_fanout_to_all_devices_and_partial_failed(
    db_session, prepared_engine, test_settings
):
    user, _, _ = await _setup_user_device_token(db_session)
    device2 = UserDevice(
        user_id=user.id, client_device_id="dev-2", platform="android"
    )
    db_session.add(device2)
    await db_session.flush()
    db_session.add(
        DevicePushToken(
            device_id=device2.id,
            provider="fcm",
            token_kind="standard",
            token_hash="hash-fcm-1",
            token_value="fcm-1",
        )
    )
    job = _job(user)
    db_session.add(job)
    await db_session.commit()

    apple = ScriptedSender([SendResult(success=True, status="200")])
    android = ScriptedSender(
        [SendResult(success=False, status="UNKNOWN", description="boom")] * 4
    )
    worker = _worker(prepared_engine, test_settings, apple=apple, android=android)

    # Round 1: apns sent, fcm transient-pending → job re-queued.
    await run_push_tick(worker)
    await db_session.refresh(job)
    assert job.status == "pending"
    assert job.attempts == 1

    # Exhaust remaining rounds (delivery max_attempts=3, job max_attempts=3).
    # Each round must also pull pending deliveries' retry backoff into the
    # past — a round that attempts nothing no longer burns job attempts.
    for _ in range(4):
        job.available_at = datetime.now(UTC) - timedelta(minutes=5)
        for d in (
            (
                await db_session.execute(
                    select(PushDelivery).where(
                        PushDelivery.push_job_id == job.id,
                        PushDelivery.status == "pending",
                    )
                )
            )
            .scalars()
            .all()
        ):
            d.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
        await db_session.commit()
        await run_push_tick(worker)
        await db_session.refresh(job)
        if job.status != "pending":
            break
    assert job.status == "partial_failed"
    deliveries = (
        (
            await db_session.execute(
                select(PushDelivery)
                .where(PushDelivery.push_job_id == job.id)
                # The loop above loaded these rows into the identity map;
                # refresh them past expire_on_commit=False staleness.
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    assert {d.status for d in deliveries} == {"sent", "failed"}


async def test_unregistered_token_invalidated(
    db_session, prepared_engine, test_settings
):
    user, _, token = await _setup_user_device_token(db_session)
    job = _job(user)
    db_session.add(job)
    await db_session.commit()

    apple = ScriptedSender(
        [SendResult(success=False, status="410", description="Unregistered")]
    )
    worker = _worker(prepared_engine, test_settings, apple=apple)
    await run_push_tick(worker)

    await db_session.refresh(job)
    assert job.status == "failed"
    await db_session.refresh(token)
    assert token.status == "invalidated"
    delivery = (
        await db_session.execute(
            select(PushDelivery).where(PushDelivery.push_job_id == job.id)
        )
    ).scalar_one()
    assert delivery.failure_code == "unregistered"


async def test_no_active_tokens_fails_job(
    db_session, prepared_engine, test_settings
):
    user, _, token = await _setup_user_device_token(
        db_session, token_kwargs={"status": "invalidated"}
    )
    job = _job(user)
    db_session.add(job)
    await db_session.commit()

    worker = _worker(prepared_engine, test_settings)
    await run_push_tick(worker)
    await db_session.refresh(job)
    assert job.status == "failed"
    assert job.last_error == "no_active_tokens"


async def test_device_targeted_job_only_hits_that_device(
    db_session, prepared_engine, test_settings
):
    user, device1, _ = await _setup_user_device_token(db_session)
    device2 = UserDevice(
        user_id=user.id, client_device_id="dev-2", platform="ios"
    )
    db_session.add(device2)
    await db_session.flush()
    db_session.add(
        DevicePushToken(
            device_id=device2.id,
            provider="apns",
            token_kind="standard",
            token_hash="hash-tok-2",
            token_value="tok-2",
        )
    )
    job = _job(user, device_id=device2.id)
    db_session.add(job)
    await db_session.commit()

    apple = ScriptedSender([SendResult(success=True, status="200")])
    worker = _worker(prepared_engine, test_settings, apple=apple)
    await run_push_tick(worker)

    assert len(apple.requests) == 1
    assert apple.requests[0].device_token == "tok-2"


async def test_live_activity_tokens_excluded(
    db_session, prepared_engine, test_settings
):
    user, device, _ = await _setup_user_device_token(db_session)
    db_session.add(
        DevicePushToken(
            device_id=device.id,
            provider="apns",
            token_kind="live_activity_update",
            token_hash="hash-la",
            token_value="la-tok",
            scope_key="assignment:1",
        )
    )
    job = _job(user)
    db_session.add(job)
    await db_session.commit()

    apple = ScriptedSender([SendResult(success=True, status="200")])
    worker = _worker(prepared_engine, test_settings, apple=apple)
    await run_push_tick(worker)
    assert len(apple.requests) == 1  # standard token only


async def test_all_skipped_after_token_dies_between_rounds(
    db_session, prepared_engine, test_settings
):
    user, _, token = await _setup_user_device_token(db_session)
    job = _job(user)
    db_session.add(job)
    await db_session.commit()

    apple = ScriptedSender(
        [SendResult(success=False, status="UNKNOWN", description="blip")] * 2
    )
    worker = _worker(prepared_engine, test_settings, apple=apple)

    # Round 1: transient failure → delivery stays pending, job re-queued.
    await run_push_tick(worker)
    await db_session.refresh(job)
    assert job.status == "pending"

    # Token dies between rounds; drive remaining rounds (also pull the
    # delivery's retry backoff into the past so the round attempts it).
    token.status = "invalidated"
    pending_delivery = (
        await db_session.execute(
            select(PushDelivery).where(PushDelivery.push_job_id == job.id)
        )
    ).scalar_one()
    for _ in range(4):
        job.available_at = datetime.now(UTC) - timedelta(minutes=5)
        pending_delivery.next_retry_at = datetime.now(UTC) - timedelta(
            seconds=1
        )
        await db_session.commit()
        await run_push_tick(worker)
        await db_session.refresh(job)
        await db_session.refresh(pending_delivery)
        if job.status != "pending":
            break

    assert job.status == "failed"
    assert job.last_error == "all_tokens_skipped"
    delivery = (
        await db_session.execute(
            select(PushDelivery).where(PushDelivery.push_job_id == job.id)
        )
    ).scalar_one()
    assert delivery.status == "skipped"


async def test_exhausted_delivery_keeps_last_transport_error(
    db_session, prepared_engine, test_settings
):
    user, _, _ = await _setup_user_device_token(db_session)
    job = _job(user)
    db_session.add(job)
    await db_session.commit()

    apple = ScriptedSender(
        [SendResult(success=False, status="UNKNOWN", description="boom")] * 5
    )
    worker = _worker(prepared_engine, test_settings, apple=apple)
    for _ in range(5):
        job.available_at = datetime.now(UTC) - timedelta(minutes=5)
        for d in (
            (
                await db_session.execute(
                    select(PushDelivery).where(
                        PushDelivery.push_job_id == job.id,
                        PushDelivery.status == "pending",
                    )
                )
            )
            .scalars()
            .all()
        ):
            d.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
        await db_session.commit()
        await run_push_tick(worker)
        await db_session.refresh(job)
        if job.status != "pending":
            break

    assert job.status == "failed"
    delivery = (
        await db_session.execute(
            select(PushDelivery)
            .where(PushDelivery.push_job_id == job.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert delivery.status == "failed"
    # The real transport error survives — not overwritten by
    # retries_exhausted bookkeeping.
    assert delivery.failure_code == "UNKNOWN"


async def test_zero_work_round_does_not_burn_final_attempt(
    db_session, prepared_engine, test_settings
):
    """A round where every pending delivery's next_retry_at is still in
    the future must not consume the job's last attempt and force-fail
    deliveries that never used theirs."""
    user, _, _ = await _setup_user_device_token(db_session)
    job = _job(user, attempts=2, max_attempts=3)
    db_session.add(job)
    await db_session.flush()
    delivery = PushDelivery(
        push_job_id=job.id,
        user_id=user.id,
        device_id=(
            await db_session.execute(select(UserDevice.id))
        ).scalar_one(),
        push_token_id=(
            await db_session.execute(select(DevicePushToken.id))
        ).scalar_one(),
        provider="apns",
        token_kind="standard",
        token_hash="hash-tok-1",
        next_retry_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    db_session.add(delivery)
    await db_session.commit()

    apple = ScriptedSender([SendResult(success=True, status="200")])
    worker = _worker(prepared_engine, test_settings, apple=apple)
    await run_push_tick(worker)

    await db_session.refresh(job)
    assert job.status == "pending"  # not failed
    assert job.attempts == 2  # final attempt not burned
    assert apple.requests == []

    # Once the retry time arrives the delivery still gets its real shot.
    delivery.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
    job.available_at = datetime.now(UTC) - timedelta(minutes=5)
    await db_session.commit()
    await run_push_tick(worker)
    await db_session.refresh(job)
    assert job.status == "sent"


# A Live Activity that is already running can only be updated or ended
# through its own update token. A push-to-start token starts activities and
# nothing else, so addressing one leaves the running activity untouched —
# which is what kept the Dynamic Island up after a class ended.


async def _add_activity_token(session, device, *, scope_key, token_value):
    token = DevicePushToken(
        device_id=device.id,
        provider="apns",
        token_kind="live_activity_update",
        token_hash=f"hash-{token_value}",
        token_value=token_value,
        bundle_id="org.ntust.app.TigerDuck",
        scope_key=scope_key,
    )
    session.add(token)
    await session.flush()
    return token


def _activity_job(user, device, *, activity_id="inClass::c1"):
    return _job(
        user,
        device_id=device.id,
        channel="schedule",
        scenario="activityEnd",
        dedupe_key=activity_end_key(device.id, activity_id),
        payload={
            "kind": "live_activity_end",
            "activity_id": activity_id,
            "source_id": "c1",
            "scenario": "inClass",
            "title": "Math",
        },
    )


async def test_activity_job_uses_the_activity_update_token(
    db_session, prepared_engine, test_settings
):
    user, device, _ = await _setup_user_device_token(db_session)
    db_session.add(
        DevicePushToken(
            device_id=device.id,
            provider="apns",
            token_kind="push_to_start",
            token_hash="hash-pts",
            token_value="pts-tok",
            bundle_id="org.ntust.app.TigerDuck",
        )
    )
    await _add_activity_token(
        db_session, device, scope_key="inClass::c1", token_value="la-tok"
    )
    db_session.add(_activity_job(user, device))
    await db_session.commit()

    apple = ScriptedSender([SendResult(success=True, status="200")])
    worker = _worker(prepared_engine, test_settings, apple=apple)
    await run_push_tick(worker)

    assert [r.device_token for r in apple.requests] == ["la-tok"]


async def test_activity_job_does_not_end_a_sibling_activity(
    db_session, prepared_engine, test_settings
):
    """Two activities on one device hold two update tokens. Only the one the
    job names may be ended."""
    user, device, _ = await _setup_user_device_token(db_session)
    await _add_activity_token(
        db_session, device, scope_key="inClass::c1", token_value="la-c1"
    )
    await _add_activity_token(
        db_session, device, scope_key="inClass::c2", token_value="la-c2"
    )
    db_session.add(_activity_job(user, device, activity_id="inClass::c1"))
    await db_session.commit()

    apple = ScriptedSender([SendResult(success=True, status="200")])
    worker = _worker(prepared_engine, test_settings, apple=apple)
    await run_push_tick(worker)

    assert [r.device_token for r in apple.requests] == ["la-c1"]


async def test_activity_job_without_activity_id_sends_nothing(
    db_session, prepared_engine, test_settings
):
    """Better a job that fails loudly than one that dismisses the wrong
    activity."""
    user, device, _ = await _setup_user_device_token(db_session)
    await _add_activity_token(
        db_session, device, scope_key="inClass::c1", token_value="la-tok"
    )
    job = _activity_job(user, device)
    job.payload = {"kind": "live_activity_end", "scenario": "inClass"}
    db_session.add(job)
    await db_session.commit()

    apple = ScriptedSender([SendResult(success=True, status="200")])
    worker = _worker(prepared_engine, test_settings, apple=apple)
    await run_push_tick(worker)

    assert apple.requests == []
    await db_session.refresh(job)
    assert job.status == "failed"
    # Named for what it is: a malformed job, not a device with no token.
    assert job.last_error == "missing_activity_id"


# A schedule job starts an activity that is not running yet, so it goes to
# the device's push-to-start token — the one kind that can start one. Unless
# the app already started that activity itself: it does so whenever it is in
# the foreground at fire time, and registers the update token as it does,
# which files the end job. While that end job is waiting, a start push would
# put a second copy on the lock screen and ring the alert for it.


def _start_job(user, device, *, activity_id="inClass::c1"):
    return _job(
        user,
        device_id=device.id,
        channel="schedule",
        scenario="inClass",
        dedupe_key=schedule_key(device.id, "c1", "inClass"),
        payload={
            "kind": "schedule",
            "scenario": "inClass",
            "source_id": "c1",
            "activity_id": activity_id,
            "title": "Math",
            "subtitle": "09:10-10:00",
            "accentHex": 1,
            "sourceId": "c1",
        },
    )


def _push_to_start_token(device, *, token_value="pts-tok", scope_key="RenamedAttributes"):
    # `scope_key` deliberately differs from the builder's default so a test
    # can tell "taken from the token" from "fell back to the constant".
    return DevicePushToken(
        device_id=device.id,
        provider="apns",
        token_kind="push_to_start",
        token_hash=f"hash-{token_value}",
        token_value=token_value,
        bundle_id="org.ntust.app.TigerDuck",
        scope_key=scope_key,
    )


def _end_job(user, device, *, activity_id, fire_at, status="pending"):
    return _job(
        user,
        device_id=device.id,
        channel="schedule",
        scenario="activityEnd",
        dedupe_key=activity_end_key(device.id, activity_id),
        fire_at=fire_at,
        status=status,
        payload={"kind": "live_activity_end", "activity_id": activity_id},
    )


async def test_start_job_uses_the_push_to_start_token(
    db_session, prepared_engine, test_settings
):
    user, device, _ = await _setup_user_device_token(db_session)
    db_session.add(_push_to_start_token(device))
    await _add_activity_token(
        db_session, device, scope_key="inClass:other", token_value="la-other"
    )
    db_session.add(_start_job(user, device))
    await db_session.commit()

    apple = ScriptedSender([SendResult(success=True, status="200")])
    worker = _worker(prepared_engine, test_settings, apple=apple)
    await run_push_tick(worker)

    assert [r.device_token for r in apple.requests] == ["pts-tok"]
    aps = apple.requests[0].message["aps"]
    assert aps["event"] == "start"
    # Taken from the token row, which is where the client says which
    # ActivityAttributes type the token starts.
    assert aps["attributes-type"] == "RenamedAttributes"
    assert aps["attributes"] == {"activityId": "inClass::c1"}


async def test_start_job_falls_back_to_the_default_attributes_type(
    db_session, prepared_engine, test_settings
):
    """A push-to-start token registered before the client filed the type
    name under scope_key still starts the app's one attributes type."""
    user, device, _ = await _setup_user_device_token(db_session)
    db_session.add(_push_to_start_token(device, scope_key=""))
    db_session.add(_start_job(user, device))
    await db_session.commit()

    apple = ScriptedSender([SendResult(success=True, status="200")])
    worker = _worker(prepared_engine, test_settings, apple=apple)
    await run_push_tick(worker)

    assert apple.requests[0].message["aps"]["attributes-type"] == "TigerDuckActivityAttributes"


async def test_start_job_is_cancelled_while_the_activity_is_registered(
    db_session, prepared_engine, test_settings
):
    user, device, _ = await _setup_user_device_token(db_session)
    db_session.add(_push_to_start_token(device))
    db_session.add(
        _end_job(
            user,
            device,
            activity_id="inClass::c1",
            fire_at=datetime.now(UTC) + timedelta(minutes=40),
        )
    )
    job = _start_job(user, device)
    db_session.add(job)
    await db_session.commit()

    apple = ScriptedSender([SendResult(success=True, status="200")])
    worker = _worker(prepared_engine, test_settings, apple=apple)
    await run_push_tick(worker)

    assert apple.requests == []
    await db_session.refresh(job)
    assert job.status == "cancelled"
    assert job.last_error == "activity_already_running"


async def _start_fires_despite(db_session, prepared_engine, test_settings, end_job):
    user, device, _ = await _setup_user_device_token(db_session)
    db_session.add(_push_to_start_token(device))
    db_session.add(end_job(user, device))
    db_session.add(_start_job(user, device))
    await db_session.commit()

    apple = ScriptedSender([SendResult(success=True, status="200")])
    worker = _worker(prepared_engine, test_settings, apple=apple)
    await run_push_tick(worker)
    assert [r.device_token for r in apple.requests] == ["pts-tok"]


async def test_start_job_fires_once_the_earlier_end_has_been_sent(
    db_session, prepared_engine, test_settings
):
    """A sent end job says the activity is gone."""
    await _start_fires_despite(
        db_session, prepared_engine, test_settings,
        lambda user, device: _end_job(
            user, device, activity_id="inClass::c1",
            fire_at=datetime.now(UTC) + timedelta(minutes=40), status="sent",
        ),
    )


async def test_start_job_fires_once_the_earlier_countdown_has_passed(
    db_session, prepared_engine, test_settings
):
    """An end job whose countdown has passed says the activity is over,
    whatever its status — a stale pending one, left by a push that never
    went out, must not block a start."""
    await _start_fires_despite(
        db_session, prepared_engine, test_settings,
        lambda user, device: _end_job(
            user, device, activity_id="inClass::c1",
            fire_at=datetime.now(UTC) - timedelta(days=7),
        ),
    )


async def test_start_job_is_not_blocked_by_another_activity(
    db_session, prepared_engine, test_settings
):
    await _start_fires_despite(
        db_session, prepared_engine, test_settings,
        lambda user, device: _end_job(
            user, device, activity_id="classPreparing::c1",
            fire_at=datetime.now(UTC) + timedelta(minutes=40),
        ),
    )


async def test_start_job_is_not_blocked_by_another_devices_activity(
    db_session, prepared_engine, test_settings
):
    """The iPad registering the activity says nothing about the iPhone."""
    async def other_devices_end_job(user, device):
        tablet = UserDevice(
            user_id=user.id, client_device_id="dev-2", platform="ipados"
        )
        db_session.add(tablet)
        await db_session.flush()
        return _end_job(
            user, tablet, activity_id="inClass::c1",
            fire_at=datetime.now(UTC) + timedelta(minutes=40),
        )

    user, device, _ = await _setup_user_device_token(db_session)
    db_session.add(_push_to_start_token(device))
    db_session.add(await other_devices_end_job(user, device))
    db_session.add(_start_job(user, device))
    await db_session.commit()

    apple = ScriptedSender([SendResult(success=True, status="200")])
    worker = _worker(prepared_engine, test_settings, apple=apple)
    await run_push_tick(worker)
    assert [r.device_token for r in apple.requests] == ["pts-tok"]


async def test_start_job_is_not_cancelled_on_a_retry_round(
    db_session, prepared_engine, test_settings
):
    """Once a delivery row exists the push may already have reached a
    token and started the activity the registration now describes;
    cancelling on the retry round would strand the other delivery as
    pending and record a sent push as never sent."""
    user, device, token = await _setup_user_device_token(db_session)
    pts = _push_to_start_token(device)
    db_session.add(pts)
    await db_session.flush()
    job = _start_job(user, device)
    job.attempts = 1
    db_session.add(job)
    await db_session.flush()
    db_session.add(
        PushDelivery(
            push_job_id=job.id,
            user_id=user.id,
            device_id=device.id,
            push_token_id=pts.id,
            provider="apns",
            token_kind="push_to_start",
            token_hash=pts.token_hash,
            scope_key=pts.scope_key,
            next_retry_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    )
    db_session.add(
        _end_job(
            user, device, activity_id="inClass::c1",
            fire_at=datetime.now(UTC) + timedelta(minutes=40),
        )
    )
    await db_session.commit()

    apple = ScriptedSender([SendResult(success=True, status="200")])
    worker = _worker(prepared_engine, test_settings, apple=apple)
    await run_push_tick(worker)

    assert [r.device_token for r in apple.requests] == ["pts-tok"]
    await db_session.refresh(job)
    assert job.status == "sent"


# UserDevice.bulletin_push_enabled gates ONLY the bulletin channel. It is
# read in exactly one place -- the token_query built above, when
# job.channel == "bulletin" -- so a device that opted out of bulletins
# must keep every other channel exactly as it was, and a device that
# never touched the flag (or explicitly turned it back on) must keep
# receiving bulletins.


async def _bulletin_flag_device(
    db_session,
    *,
    bulletin_push_enabled: bool | None = None,
    client_device_id: str = "dev-bulletin-flag",
    app_version: str = "2.1.0",
):
    user = User(student_id="b11203058")
    db_session.add(user)
    await db_session.flush()
    device_kwargs = dict(
        user_id=user.id,
        client_device_id=client_device_id,
        platform="ios",
        app_version=app_version,
    )
    # Omitting the kwarg entirely (rather than passing False/True) is what
    # stands in for "a device that never sent this field" -- the column's
    # own default applies, exactly as it would for a pre-migration row.
    if bulletin_push_enabled is not None:
        device_kwargs["bulletin_push_enabled"] = bulletin_push_enabled
    device = UserDevice(**device_kwargs)
    db_session.add(device)
    await db_session.flush()
    db_session.add(
        DevicePushToken(
            device_id=device.id,
            provider="apns",
            token_kind="standard",
            token_hash=f"hash-{client_device_id}",
            token_value=f"tok-{client_device_id}",
            bundle_id="org.ntust.app.TigerDuck",
        )
    )
    db_session.add(_push_to_start_token(device, token_value=f"pts-{client_device_id}"))
    await db_session.flush()
    return user, device


async def _delivered_device_ids_for_job(session, job_id) -> set:
    rows = (
        await session.execute(
            select(PushDelivery.device_id).where(PushDelivery.push_job_id == job_id)
        )
    ).scalars().all()
    return set(rows)


async def test_bulletin_flag_false_blocks_only_the_bulletin_channel(
    db_session, prepared_engine, test_settings
):
    """One opted-out device, three jobs: the bulletin is withheld, and an
    assignment reminder and a server-started Live Activity still land."""
    user, device = await _bulletin_flag_device(db_session, bulletin_push_enabled=False)

    bulletin_job = _job(
        user,
        channel="bulletin",
        scenario="bulletin_matched",
        dedupe_key="bulletin:test:flag-false",
    )
    reminder_job = _job(
        user,
        channel=REMINDER_CHANNEL,
        scenario="reminder_24h",
        dedupe_key="assignment:test:flag-false",
    )
    activity_job = _start_job(user, device)
    db_session.add_all([bulletin_job, reminder_job, activity_job])
    await db_session.commit()

    worker = _worker(prepared_engine, test_settings, apple=ScriptedSender())
    await run_push_tick(worker)

    assert await _delivered_device_ids_for_job(db_session, bulletin_job.id) == set()
    assert device.id in await _delivered_device_ids_for_job(db_session, reminder_job.id)
    assert device.id in await _delivered_device_ids_for_job(db_session, activity_job.id)


@pytest.mark.parametrize("bulletin_push_enabled", [True, None], ids=["true", "unset"])
async def test_bulletin_flag_true_or_unset_receives_bulletin(
    db_session, prepared_engine, test_settings, bulletin_push_enabled
):
    """True (explicitly re-enabled) and unset (a device that predates the
    column, or a pre-2.1.0 client that never sends the field) both resolve
    to "deliver" -- only an explicit false withholds the bulletin."""
    user, device = await _bulletin_flag_device(
        db_session,
        bulletin_push_enabled=bulletin_push_enabled,
        client_device_id=f"dev-bulletin-{bulletin_push_enabled}",
    )
    assert device.bulletin_push_enabled is True

    job = _job(
        user,
        channel="bulletin",
        scenario="bulletin_matched",
        dedupe_key=f"bulletin:test:flag-{bulletin_push_enabled}",
    )
    db_session.add(job)
    await db_session.commit()

    worker = _worker(prepared_engine, test_settings, apple=ScriptedSender())
    await run_push_tick(worker)

    assert device.id in await _delivered_device_ids_for_job(db_session, job.id)
