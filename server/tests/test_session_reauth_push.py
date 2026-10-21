"""Asking a device left without a TigerSync session to sign in again
(`server/push/session_reauth.py`, tigerduck-backend#125)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from server.auth.models import (
    AuthSession,
    DevicePushToken,
    PushDelivery,
    PushJob,
    User,
    UserDevice,
)
from server.db import build_engine, build_session_factory
from server.i18n import translate
from server.models import DeviceRegistration
from server.push import pipeline
from server.push.apns_client import RecordingSender, SendResult
from server.push.fcm_client import RecordingFcmSender
from server.push.notification_copy import REAUTH_SCENARIO, SESSION_REAUTH_REASON
from server.push.pipeline import PushPipelineWorker, run_push_tick
from server.push.router import PushRouter
from server.push.session_reauth import scan_sessionless_devices
from server.scheduler.runtime import build_scheduler

asyncio_session = pytest.mark.asyncio(loop_scope="session")

TAIPEI = ZoneInfo("Asia/Taipei")
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
# Noon at school today: the scan files pushes only in the daytime there.
NOW = (
    datetime.now(TAIPEI)
    .replace(hour=12, minute=0, second=0, microsecond=0)
    .astimezone(UTC)
)
# The pipeline's clock in these tests: in the daytime at school, where the
# push may go out, a few minutes after the scan.
SENT_AT = NOW + 5 * MINUTE
TITLE_KEY = "notification_reauth_session_title"
BODY_KEY = "notification_reauth_session_body"


def _at_school(hour: int, minute: int = 0) -> datetime:
    return NOW.astimezone(TAIPEI).replace(hour=hour, minute=minute).astimezone(UTC)


def live(issued_ago: timedelta) -> dict:
    return {"issued_at": NOW - issued_ago, "expires_at": NOW - issued_ago + 90 * DAY}


def revoked(ago: timedelta, reason: str) -> dict:
    return {
        "issued_at": NOW - ago - HOUR,
        "expires_at": NOW + 80 * DAY,
        "revoked_at": NOW - ago,
        "revoked_reason": reason,
    }


async def _device(
    session,
    label: str,
    *,
    sessions: list[dict],
    user: User | None = None,
    platform: str = "ios",
    announced: datetime | None = NOW - 15 * MINUTE,
    token: str | None = "standard",
    token_name: str | None = None,
    token_status: str = "active",
    token_expires_at: datetime | None = None,
    **fields,
) -> UserDevice:
    """A device of `user`, a user of its own by default, holding `sessions`
    in the order given (the newest last), a push token of kind `token`
    (None for none) named `token_name` (the label by default), and last
    announced at `announced` (None for never)."""
    if user is None:
        user = User()
        session.add(user)
        await session.flush()
    device = UserDevice(
        user_id=user.id, client_device_id=label, platform=platform, **fields
    )
    session.add(device)
    await session.flush()
    for index, values in enumerate(sessions):
        session.add(
            AuthSession(
                user_id=user.id,
                device_id=device.id,
                refresh_token_hash=f"{device.id}-{index}",
                **values,
            )
        )
        # One at a time, so the ids follow the list.
        await session.flush()
    if token is not None:
        session.add(
            DevicePushToken(
                device_id=device.id,
                provider="fcm" if platform == "android" else "apns",
                token_kind=token,
                token_hash=f"hash-{token_name or label}",
                token_value=f"tok-{token_name or label}",
                bundle_id="org.ntust.app.TigerDuck",
                status=token_status,
                expires_at=token_expires_at,
            )
        )
    if announced is not None:
        session.add(
            DeviceRegistration(
                device_id=label,
                user_id=f"anon-{label}",
                platform="android" if platform == "android" else "apple",
                pts_token_hex="",
                bundle_id="",
                attrs_type="",
                apns_env="",
                updated_at=announced,
            )
        )
    await session.commit()
    return device


async def _announce(session, label: str, at: datetime) -> None:
    await session.execute(
        update(DeviceRegistration)
        .where(DeviceRegistration.device_id == label)
        .values(updated_at=at)
    )
    await session.commit()


async def _sign_in_again(session, device: UserDevice, at: datetime) -> None:
    session.add(
        AuthSession(
            user_id=device.user_id,
            device_id=device.id,
            refresh_token_hash=f"relogin-{at.isoformat()}",
            issued_at=at,
            expires_at=at + 90 * DAY,
        )
    )
    await session.commit()


CHANGES = ["signed in again", "logout", "removed", "TigerSync off"]


async def _change(session, device: UserDevice, change: str) -> None:
    now = SENT_AT
    if change == "signed in again":
        await _sign_in_again(session, device, now)
        return
    if change == "logout":
        statement = (
            update(AuthSession)
            .where(AuthSession.device_id == device.id)
            .values(revoked_at=now, revoked_reason="logout")
        )
    elif change == "removed":
        statement = (
            update(UserDevice).where(UserDevice.id == device.id).values(deleted_at=now)
        )
    else:
        statement = (
            update(UserDevice)
            .where(UserDevice.id == device.id)
            .values(cloud_sync_enabled=False)
        )
    await session.execute(statement)
    await session.commit()


async def _scan(engine, settings, at: datetime = NOW) -> int:
    return await scan_sessionless_devices(
        build_session_factory(engine), settings, now=at
    )


async def _jobs(session) -> dict[str, list[PushJob]]:
    """The jobs filed, by the label of the device each one is for."""
    rows = await session.execute(
        select(UserDevice.client_device_id, PushJob)
        .join(UserDevice, UserDevice.id == PushJob.device_id)
        .order_by(PushJob.id)
        .execution_options(populate_existing=True)
    )
    jobs: dict[str, list[PushJob]] = {}
    for label, job in rows:
        jobs.setdefault(label, []).append(job)
    return jobs


class ScriptedSender:
    """Records each request and answers with the next of `results`, the
    last one repeating."""

    def __init__(self, *results: SendResult) -> None:
        self.results = list(results) or [SendResult(success=True, status="200")]
        self.requests = []

    async def send(self, request):
        self.requests.append(request)
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]

    async def send_multi(self, requests):
        return [await self.send(r) for r in requests]

    async def close(self):
        pass


def _clock(at: datetime):
    """`datetime` for the pipeline module, its `now` fixed at `at`."""

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return at.astimezone(tz)

    return Clock


async def _tick(session, engine, settings, apple=None, android=None, at=SENT_AT):
    """Make every pending job and delivery due at `at`, and run one push tick
    with the pipeline's clock there."""
    past = at - 5 * MINUTE
    await session.execute(
        update(PushJob)
        .where(PushJob.status == "pending")
        .values(fire_at=past, available_at=past)
    )
    await session.execute(
        update(PushDelivery)
        .where(PushDelivery.status == "pending")
        .values(next_retry_at=past)
    )
    await session.commit()
    apple = apple or ScriptedSender()
    android = android or ScriptedSender()
    with patch.object(pipeline, "datetime", _clock(at)):
        await run_push_tick(
            PushPipelineWorker(
                session_factory=build_session_factory(engine),
                settings=settings,
                router=PushRouter(apple=apple, android=android),
                worker_id="session-reauth-test",
            )
        )
    return apple, android


# --- Who is asked ------------------------------------------------------------


@asyncio_session
async def test_only_a_device_left_without_a_session_is_asked(
    db_session, prepared_engine, test_settings
):
    superseded_rival = {
        "issued_at": NOW - 30 * HOUR,
        "expires_at": NOW + 60 * DAY,
        "revoked_at": NOW - 29 * HOUR,
        "revoked_reason": "superseded",
    }
    asked = {
        "theft-revoked": [revoked(2 * HOUR, "reuse_detected")],
        "expired-on-refresh": [revoked(2 * HOUR, "expired")],
        "revoked-by-operator": [revoked(2 * HOUR, "admin_revoked")],
        "expired-unrefreshed": [
            {"issued_at": NOW - 92 * DAY, "expires_at": NOW - 2 * DAY}
        ],
        "live-untouched": [live(30 * HOUR)],
        # The newest of two sessions a second rotation left was superseded,
        # and the one that won has not been refreshed since.
        "rival-untouched": [live(31 * HOUR), superseded_rival],
    }
    left_alone = {
        "logged-out": [revoked(2 * HOUR, "logout")],
        "logged-out-rival-live": [live(40 * HOUR), revoked(2 * HOUR, "logout")],
        "used-today": [live(2 * HOUR)],
        "live-under-a-day": [live(20 * HOUR)],
        "revoked-within-the-hour": [revoked(30 * MINUTE, "reuse_detected")],
        "signed-in-again": [revoked(5 * HOUR, "reuse_detected"), live(HOUR)],
        # Superseded over an hour ago by a session in use since.
        "rival-in-use": [
            live(3 * HOUR),
            {
                **superseded_rival,
                "issued_at": NOW - 150 * MINUTE,
                "revoked_at": NOW - 2 * HOUR,
            },
        ],
        "no-sessions": [],
    }
    for label, sessions in {**asked, **left_alone}.items():
        await _device(db_session, label, sessions=sessions)

    gone = [revoked(2 * HOUR, "reuse_detected")]
    left_alone_by_device = {
        "not-seen-in-3-days": {
            "sessions": [revoked(5 * DAY, "reuse_detected")],
            "announced": NOW - 4 * DAY,
        },
        "never-announced": {"sessions": gone, "announced": None},
        "deleted": {"sessions": gone, "deleted_at": NOW - DAY},
        "sync-off": {"sessions": gone, "cloud_sync_enabled": False},
        "mac": {"sessions": gone, "platform": "macos"},
        "no-token": {"sessions": gone, "token": None},
        "start-token-only": {"sessions": gone, "token": "push_to_start"},
        "token-invalidated": {"sessions": gone, "token_status": "invalidated"},
        "token-expired": {"sessions": gone, "token_expires_at": NOW - HOUR},
    }
    for label, kwargs in left_alone_by_device.items():
        await _device(db_session, label, **kwargs)
    await _device(db_session, "ipad", sessions=gone, platform="ipados")
    await _device(db_session, "pixel", sessions=[live(30 * HOUR)], platform="android")

    assert await _scan(prepared_engine, test_settings) == len(asked) + 2

    jobs = await _jobs(db_session)
    assert set(jobs) == set(asked) | {"ipad", "pixel"}
    for label, [job] in jobs.items():
        device = (
            await db_session.execute(
                select(UserDevice).where(UserDevice.client_device_id == label)
            )
        ).scalar_one()
        assert job.user_id == device.user_id
        assert job.channel == "system"
        assert job.scenario == REAUTH_SCENARIO
        # The copy is written per recipient at send time.
        assert job.payload == {"kind": REAUTH_SCENARIO, "reason": SESSION_REAUTH_REASON}


@asyncio_session
async def test_each_rule_holds_to_its_boundary(
    db_session, prepared_engine, test_settings
):
    seen = NOW - 15 * MINUTE

    def ended(at: datetime) -> dict:
        return {
            "issued_at": at - HOUR,
            "expires_at": NOW + 80 * DAY,
            "revoked_at": at,
            "revoked_reason": "reuse_detected",
        }

    def issued(at: datetime) -> dict:
        return {"issued_at": at, "expires_at": at + 90 * DAY}

    # label: (sessions, last announce, asked)
    cases = {
        "ended-61m-before": ([ended(seen - 61 * MINUTE)], seen, True),
        "ended-59m-before": ([ended(seen - 59 * MINUTE)], seen, False),
        "issued-24h1m-before": ([issued(seen - 24 * HOUR - MINUTE)], seen, True),
        "issued-23h59m-before": ([issued(seen - 24 * HOUR + MINUTE)], seen, False),
        "seen-3d-less-1m": ([ended(NOW - 4 * DAY)], NOW - 3 * DAY + MINUTE, True),
        "seen-3d-and-1m": ([ended(NOW - 4 * DAY)], NOW - 3 * DAY - MINUTE, False),
        # The app refreshes, or signs in again, right after it announces.
        "revoked-seen-11m": ([ended(NOW - 2 * HOUR)], NOW - 11 * MINUTE, True),
        "revoked-seen-9m": ([ended(NOW - 2 * HOUR)], NOW - 9 * MINUTE, False),
        "live-seen-11m": ([issued(NOW - 30 * HOUR)], NOW - 11 * MINUTE, True),
        "live-seen-9m": ([issued(NOW - 30 * HOUR)], NOW - 9 * MINUTE, False),
    }
    for label, (sessions, announced, _) in cases.items():
        await _device(db_session, label, sessions=sessions, announced=announced)

    await _scan(prepared_engine, test_settings)
    asked = {label for label, (_, _, asked) in cases.items() if asked}
    assert set(await _jobs(db_session)) == asked


def _ring(rivals: int, first: dict) -> list[dict]:
    """The sessions a duplicate refresh left as `rivals` of one another
    (`auth.service._rotate_again`): the oldest, `first`, was used first and
    superseded the newer ones (`auth.service.settle_contest`)."""
    return [first] + [
        {
            "issued_at": first["issued_at"] + index * MINUTE,
            "expires_at": NOW + 80 * DAY,
            "revoked_at": first["issued_at"] + HOUR,
            "revoked_reason": "superseded",
        }
        for index in range(1, rivals)
    ]


@asyncio_session
@pytest.mark.parametrize("rivals", [2, 3])
@pytest.mark.parametrize(("ending", "asked"), [("logout", False), ("expired", True)])
async def test_a_ring_is_judged_by_the_session_that_won_it(
    db_session, prepared_engine, test_settings, rivals, ending, asked
):
    won = {
        "issued_at": NOW - 6 * HOUR,
        "expires_at": NOW - 3 * HOUR if ending == "expired" else NOW + 80 * DAY,
        "revoked_at": NOW - 2 * HOUR,
        "revoked_reason": ending,
    }
    await _device(db_session, "iphone", sessions=_ring(rivals, won))

    assert await _scan(prepared_engine, test_settings) == int(asked)


@asyncio_session
async def test_two_accounts_on_one_device_id_are_kept_apart(
    db_session, prepared_engine, test_settings
):
    gone = [revoked(2 * HOUR, "reuse_detected")]
    # The account that left the phone kept a token of its own, a stale one.
    await _device(db_session, "shared-1", sessions=gone, token_name="left-1")
    await _device(
        db_session, "shared-1", sessions=[live(HOUR)], token_name="now-1", announced=None
    )
    # Here its token moved to the account on the phone now.
    await _device(db_session, "shared-2", sessions=gone, token=None)
    await _device(
        db_session, "shared-2", sessions=[live(HOUR)], token_name="now-2", announced=None
    )

    assert await _scan(prepared_engine, test_settings) == 1
    apple, _ = await _tick(db_session, prepared_engine, test_settings)
    assert [r.device_token for r in apple.requests] == ["tok-left-1"]


@asyncio_session
async def test_only_the_sessionless_device_of_a_user_receives_it(
    db_session, prepared_engine, test_settings
):
    user = User()
    db_session.add(user)
    await db_session.flush()
    gone = [revoked(2 * HOUR, "reuse_detected")]
    await _device(db_session, "iphone", user=user, sessions=gone, locale="zh-Hant-TW")
    for label, platform in (("ipad", "ipados"), ("pixel", "android")):
        await _device(
            db_session, label, user=user, sessions=[live(HOUR)], platform=platform
        )

    assert await _scan(prepared_engine, test_settings) == 1
    apple, android = await _tick(db_session, prepared_engine, test_settings)

    assert android.requests == []
    [request] = apple.requests
    assert request.device_token == "tok-iphone"
    assert request.message["aps"]["alert"] == {
        "title": translate(TITLE_KEY, "zh-Hant"),
        "body": translate(BODY_KEY, "zh-Hant"),
    }
    assert request.message["kind"] == REAUTH_SCENARIO
    assert request.message["reason"] == SESSION_REAUTH_REASON
    [job] = (await _jobs(db_session))["iphone"]
    assert job.status == "sent"


@asyncio_session
async def test_an_android_device_gets_the_copy_in_its_language(
    db_session, prepared_engine, test_settings
):
    await _device(
        db_session,
        "pixel",
        sessions=[live(30 * HOUR)],
        platform="android",
        locale="ja-JP",
    )

    assert await _scan(prepared_engine, test_settings) == 1
    apple, android = await _tick(db_session, prepared_engine, test_settings)

    assert apple.requests == []
    [request] = android.requests
    assert request.token == "tok-pixel"
    assert (request.title, request.body) == (
        translate(TITLE_KEY, "ja"),
        translate(BODY_KEY, "ja"),
    )
    assert request.data["kind"] == REAUTH_SCENARIO
    assert request.data["reason"] == SESSION_REAUTH_REASON
    assert request.data["android_channel_id"] == "system"
    # Signed out, the app shows nothing for it.
    assert request.priority == "normal"


# --- How often ---------------------------------------------------------------


@asyncio_session
async def test_a_device_is_asked_at_most_once_every_seven_days(
    db_session, prepared_engine, test_settings
):
    device = await _device(
        db_session, "iphone", sessions=[revoked(2 * HOUR, "reuse_detected")]
    )
    assert await _scan(prepared_engine, test_settings) == 1

    # Still without a session and still opened, as after a restart: the
    # limit lives in the database.
    for at in (NOW + HOUR, NOW + 7 * DAY - HOUR):
        await _announce(db_session, "iphone", at - 15 * MINUTE)
        engine = build_engine(test_settings)
        try:
            assert await _scan(engine, test_settings, at) == 0
        finally:
            await engine.dispose()

    # The first push went out; a sent job does not hold the next one back.
    await db_session.execute(update(PushJob).values(status="sent"))
    await db_session.commit()
    again = NOW + 7 * DAY + 10 * MINUTE
    await _announce(db_session, "iphone", again - 15 * MINUTE)
    assert await _scan(prepared_engine, test_settings, again) == 1

    jobs = (await _jobs(db_session))["iphone"]
    assert len(jobs) == 2
    assert jobs[0].dedupe_key != jobs[1].dedupe_key
    await db_session.refresh(device)
    assert device.session_reauth_pushed_at == again


@asyncio_session
async def test_overlapping_scans_file_one_job_per_device(
    db_session, prepared_engine, test_settings
):
    labels = [f"phone-{index}" for index in range(40)]
    for label in labels:
        await _device(db_session, label, sessions=[revoked(2 * HOUR, "reuse_detected")])

    # A second apart, as on two hosts, so the jobs' dedupe keys differ and
    # only the device's mark keeps them apart.
    factory = build_session_factory(prepared_engine)
    created = await asyncio.gather(
        *(
            scan_sessionless_devices(
                factory, test_settings, now=NOW + timedelta(seconds=index)
            )
            for index in range(4)
        )
    )

    assert sum(created) == len(labels)
    jobs = await _jobs(db_session)
    assert sorted(jobs) == sorted(labels)
    assert all(len(filed) == 1 for filed in jobs.values())


@asyncio_session
async def test_a_device_that_signed_in_again_is_left_alone(
    db_session, prepared_engine, test_settings
):
    device = await _device(
        db_session, "iphone", sessions=[revoked(2 * HOUR, "reuse_detected")]
    )
    assert await _scan(prepared_engine, test_settings) == 1

    # Past the 7 days, with the session it signed in with in use since.
    later = NOW + 8 * DAY
    await _sign_in_again(db_session, device, later - HOUR)
    await _announce(db_session, "iphone", later - 15 * MINUTE)
    assert await _scan(prepared_engine, test_settings, later) == 0
    assert len((await _jobs(db_session))["iphone"]) == 1


@asyncio_session
async def test_a_device_is_no_longer_asked_four_weeks_after_its_session_ended(
    db_session, prepared_engine, test_settings
):
    await _device(db_session, "ended-28d", sessions=[revoked(28 * DAY, "expired")])
    await _device(
        db_session, "ended-28d-1m", sessions=[revoked(28 * DAY + MINUTE, "expired")]
    )
    await _device(
        db_session,
        "expired-29d",
        sessions=[{"issued_at": NOW - 119 * DAY, "expires_at": NOW - 29 * DAY}],
    )
    # A live session left unused is asked on, however old.
    await _device(db_session, "live-untouched-40d", sessions=[live(40 * DAY)])

    assert await _scan(prepared_engine, test_settings) == 2
    assert set(await _jobs(db_session)) == {"ended-28d", "live-untouched-40d"}


@asyncio_session
async def test_a_device_that_signs_in_again_after_the_cap_is_asked_again(
    db_session, prepared_engine, test_settings
):
    device = await _device(
        db_session, "iphone", sessions=[revoked(30 * DAY, "reuse_detected")]
    )
    assert await _scan(prepared_engine, test_settings) == 0

    # Signed in again, and used it until its session went again two hours ago.
    db_session.add(
        AuthSession(
            user_id=device.user_id,
            device_id=device.id,
            refresh_token_hash="relogin",
            **revoked(2 * HOUR, "reuse_detected"),
        )
    )
    await db_session.commit()
    assert await _scan(prepared_engine, test_settings) == 1
    assert len((await _jobs(db_session))["iphone"]) == 1


@asyncio_session
async def test_nothing_is_filed_at_night_at_school(
    db_session, prepared_engine, test_settings
):
    night = _at_school(3)
    device = await _device(
        db_session,
        "iphone",
        sessions=[
            {
                "issued_at": night - 3 * HOUR,
                "expires_at": night + 80 * DAY,
                "revoked_at": night - 2 * HOUR,
                "revoked_reason": "reuse_detected",
            }
        ],
        announced=night - 10 * MINUTE,
    )

    for at in (night, _at_school(8, 59), _at_school(21)):
        assert await _scan(prepared_engine, test_settings, at) == 0
    await db_session.refresh(device)
    assert device.session_reauth_pushed_at is None

    assert await _scan(prepared_engine, test_settings, _at_school(9)) == 1


# --- At send time --------------------------------------------------------------


@asyncio_session
async def test_a_push_filed_before_the_device_signed_in_again_is_cancelled(
    db_session, prepared_engine, test_settings
):
    device = await _device(
        db_session, "iphone", sessions=[revoked(2 * HOUR, "reuse_detected")]
    )
    assert await _scan(prepared_engine, test_settings) == 1

    await _sign_in_again(db_session, device, SENT_AT - MINUTE)
    apple, _ = await _tick(db_session, prepared_engine, test_settings)

    assert apple.requests == []
    [job] = (await _jobs(db_session))["iphone"]
    assert (job.status, job.last_error) == ("cancelled", "stale_before_send")


@asyncio_session
@pytest.mark.parametrize("change", CHANGES)
async def test_a_push_is_cancelled_when_the_device_changes_before_it_goes_out(
    db_session, prepared_engine, test_settings, change
):
    device = await _device(db_session, "iphone", sessions=[live(30 * HOUR)])
    assert await _scan(prepared_engine, test_settings) == 1

    await _change(db_session, device, change)
    apple, _ = await _tick(db_session, prepared_engine, test_settings)

    assert apple.requests == []
    [job] = (await _jobs(db_session))["iphone"]
    assert (job.status, job.last_error) == ("cancelled", "stale_before_send")


@asyncio_session
@pytest.mark.parametrize("change", CHANGES)
async def test_a_retry_after_the_device_changed_sends_nothing(
    db_session, prepared_engine, test_settings, change
):
    device = await _device(db_session, "iphone", sessions=[live(30 * HOUR)])
    assert await _scan(prepared_engine, test_settings) == 1
    # The first try does not get through; the retry would.
    apple = ScriptedSender(
        SendResult(success=False, status="500"), SendResult(success=True, status="200")
    )
    await _tick(db_session, prepared_engine, test_settings, apple=apple)

    await _change(db_session, device, change)
    await _tick(db_session, prepared_engine, test_settings, apple=apple)

    assert len(apple.requests) == 1
    [job] = (await _jobs(db_session))["iphone"]
    assert (job.status, job.last_error) == ("cancelled", "stale_before_send")
    [delivery] = (
        await db_session.execute(
            select(PushDelivery)
            .where(PushDelivery.push_job_id == job.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    assert (delivery.status, delivery.failure_code) == ("skipped", "stale_before_send")


def _filed_at(at: datetime) -> list[dict]:
    """A device's sessions as a scan at `at` asks it: gone two hours."""
    return [
        {
            "issued_at": at - 3 * HOUR,
            "expires_at": at + 80 * DAY,
            "revoked_at": at - 2 * HOUR,
            "revoked_reason": "reuse_detected",
        }
    ]


async def _job(session) -> PushJob:
    [job] = (
        await session.execute(
            select(PushJob).execution_options(populate_existing=True)
        )
    ).scalars().all()
    return job


@asyncio_session
async def test_a_retry_due_after_nine_at_night_waits_for_the_morning(
    db_session, prepared_engine, test_settings
):
    filed = _at_school(20, 58)
    morning = _at_school(9) + DAY
    await _device(
        db_session, "iphone", sessions=_filed_at(filed), announced=filed - 15 * MINUTE
    )
    assert await _scan(prepared_engine, test_settings, filed) == 1
    apple = ScriptedSender(
        SendResult(success=False, status="500"), SendResult(success=True, status="200")
    )
    # The first try fails at 20:59, and its retry comes due after 21:00.
    for at in (_at_school(20, 59), _at_school(21) + timedelta(seconds=30)):
        await _tick(db_session, prepared_engine, test_settings, apple, at=at)
    assert len(apple.requests) == 1
    job = await _job(db_session)
    assert (job.status, job.attempts) == ("pending", 1)
    assert job.available_at == morning

    await _tick(db_session, prepared_engine, test_settings, apple, at=morning)
    assert len(apple.requests) == 2
    assert (await _job(db_session)).status == "sent"


@asyncio_session
async def test_a_job_left_pending_over_a_restart_at_night_waits_for_the_morning(
    db_session, prepared_engine, test_settings
):
    filed = _at_school(20, 59)
    morning = _at_school(9) + DAY
    await _device(
        db_session, "iphone", sessions=_filed_at(filed), announced=filed - 15 * MINUTE
    )
    assert await _scan(prepared_engine, test_settings, filed) == 1

    # Claimed first by the backend that came up at 23:30, and again at 05:00.
    apple = ScriptedSender()
    for at in (_at_school(23, 30), _at_school(5) + DAY):
        await _tick(db_session, prepared_engine, test_settings, apple, at=at)
        job = await _job(db_session)
        assert (job.status, job.attempts) == ("pending", 0)
        assert job.available_at == morning
    assert apple.requests == []

    await _tick(db_session, prepared_engine, test_settings, apple, at=morning)
    assert [r.device_token for r in apple.requests] == ["tok-iphone"]


@asyncio_session
@pytest.mark.parametrize("refreshed", [False, True], ids=["still-gone", "refreshed"])
async def test_a_launch_right_before_the_push_holds_it_until_the_app_refreshed(
    db_session, prepared_engine, test_settings, refreshed
):
    device = await _device(
        db_session, "iphone", sessions=[revoked(2 * HOUR, "reuse_detected")]
    )
    assert await _scan(prepared_engine, test_settings) == 1

    launched = SENT_AT - MINUTE
    await _announce(db_session, "iphone", launched)
    apple, _ = await _tick(db_session, prepared_engine, test_settings)
    assert apple.requests == []
    job = await _job(db_session)
    assert (job.status, job.attempts) == ("pending", 0)
    assert job.available_at == launched + 10 * MINUTE

    if refreshed:
        await _sign_in_again(db_session, device, launched + MINUTE)
    apple, _ = await _tick(
        db_session, prepared_engine, test_settings, at=launched + 10 * MINUTE
    )
    job = await _job(db_session)
    if refreshed:
        assert apple.requests == []
        assert (job.status, job.last_error) == ("cancelled", "stale_before_send")
    else:
        assert len(apple.requests) == 1
        assert job.status == "sent"


@asyncio_session
@pytest.mark.parametrize("rivals", [2, 3])
async def test_a_logout_on_the_session_that_won_a_ring_cancels_the_push(
    db_session, prepared_engine, test_settings, rivals
):
    # Left unused for 30 hours, then used once more, only to sign out.
    won = {"issued_at": NOW - 30 * HOUR, "expires_at": NOW + 60 * DAY}
    device = await _device(db_session, "iphone", sessions=_ring(rivals, won))
    assert await _scan(prepared_engine, test_settings) == 1

    await db_session.execute(
        update(AuthSession)
        .where(AuthSession.device_id == device.id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=SENT_AT - MINUTE, revoked_reason="logout")
    )
    await db_session.commit()
    apple, _ = await _tick(db_session, prepared_engine, test_settings)

    assert apple.requests == []
    assert (await _job(db_session)).last_error == "stale_before_send"


@asyncio_session
async def test_a_push_whose_device_row_went_before_it_was_sent_is_cancelled(
    db_session, prepared_engine, test_settings
):
    user = User()
    db_session.add(user)
    await db_session.flush()
    iphone = await _device(
        db_session, "iphone", user=user, sessions=[revoked(2 * HOUR, "reuse_detected")]
    )
    await _device(
        db_session, "ipad", user=user, sessions=[live(HOUR)], platform="ipados"
    )
    assert await _scan(prepared_engine, test_settings) == 1
    [job] = (await _jobs(db_session))["iphone"]

    # Another account signs in on the iPhone (`auth.service._upsert_device`).
    await db_session.delete(iphone)
    await db_session.commit()
    apple, android = await _tick(db_session, prepared_engine, test_settings)

    assert apple.requests == [] and android.requests == []
    job = await db_session.get(PushJob, job.id, populate_existing=True)
    assert (job.status, job.last_error) == ("cancelled", "stale_before_send")


@asyncio_session
async def test_a_push_whose_device_is_gone_reaches_no_other_device(
    db_session, prepared_engine, test_settings
):
    user = User()
    db_session.add(user)
    await db_session.flush()
    iphone = await _device(
        db_session, "iphone", user=user, sessions=[revoked(2 * HOUR, "reuse_detected")]
    )
    await _device(
        db_session, "ipad", user=user, sessions=[live(HOUR)], platform="ipados"
    )
    assert await _scan(prepared_engine, test_settings) == 1
    [job] = (await _jobs(db_session))["iphone"]

    # The first round fails, then another account signs in on the iPhone,
    # and its login deletes this account's row of it
    # (`auth.service._upsert_device`).
    apple = ScriptedSender(SendResult(success=False, status="500"))
    await _tick(db_session, prepared_engine, test_settings, apple=apple)
    await db_session.delete(iphone)
    await db_session.commit()
    await _tick(db_session, prepared_engine, test_settings, apple=apple)

    assert [r.device_token for r in apple.requests] == ["tok-iphone"]
    deliveries = (
        await db_session.execute(
            select(PushDelivery).where(PushDelivery.push_job_id == job.id)
        )
    ).scalars().all()
    assert len(deliveries) == 1


# --- Wiring --------------------------------------------------------------------


def _scheduled_scan(settings):
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
    return scheduler.get_job("session_reauth_scan")


def test_the_scan_is_scheduled_with_the_push_pipeline(test_settings):
    job = _scheduled_scan(test_settings)
    assert job is not None
    assert job.trigger.interval == timedelta(
        seconds=test_settings.session_reauth_scan_interval_seconds
    )


def test_an_interval_of_zero_turns_the_scan_off(test_settings):
    off = test_settings.model_copy(update={"session_reauth_scan_interval_seconds": 0})
    assert _scheduled_scan(off) is None
