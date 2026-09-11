"""The reauth push must carry real, localized copy.

Regression guard: the shipped payload was {"reason", "provider"} with no
title/body/kind. `build_apns_for_job` turns a missing title into "" without
complaining, so iOS showed an empty banner that still rang, and Android's
FcmService dropped the message entirely. The existing pipeline test passed
because it injected its own title/body — it never exercised this payload.

Second half: the delivery conditions of spec §4.5. The job is queued only
when the account has an iPhone or iPad with course sync on — nothing else
can repair the credential — and macOS never receives it. The copy tests
above prove the string is right; these prove it reaches the right devices
in the right language.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from server.auth.models import (
    DevicePushToken,
    ExternalAccount,
    PushDelivery,
    PushJob,
    User,
    UserDevice,
)
from server.db import build_session_factory
from server.i18n import MISSING_KEY_SENTINEL, translate
from server.push.apns_client import SendResult
from server.push.job_payloads import build_apns_for_job, build_fcm_for_job
from server.push.pipeline import PushPipelineWorker, run_push_tick
from server.push.router import PushRouter
from server.syncjobs.credentials import (
    REAUTH_SCENARIO,
    build_reauth_payload,
    mark_credentials_invalid,
)

# Applied per-function below rather than as a module-level `pytestmark`:
# the copy tests are plain sync functions that touch no fixture and need no
# event loop, and pytest-asyncio warns when the asyncio mark reaches one.
asyncio_session = pytest.mark.asyncio(loop_scope="session")


def test_payload_carries_routing_kind():
    payload = build_reauth_payload(provider="moodle", locale="en")
    assert payload["kind"] == "reauth_required"
    assert payload["provider"] == "moodle"


@pytest.mark.parametrize("locale", ["en", "zh-Hant", "ja", None, "xx"])
def test_payload_title_and_body_are_never_empty(locale):
    payload = build_reauth_payload(provider="moodle", locale=locale)
    assert payload["title"].strip()
    assert payload["body"].strip()
    assert payload["title"] != MISSING_KEY_SENTINEL
    assert payload["body"] != MISSING_KEY_SENTINEL


def test_payload_is_localized():
    en = build_reauth_payload(provider="moodle", locale="en")
    zh = build_reauth_payload(provider="moodle", locale="zh-Hant")
    assert en["title"] != zh["title"]
    assert en["body"] != zh["body"]


def test_apns_alert_is_not_empty():
    payload = build_reauth_payload(provider="moodle", locale="zh-Hant")
    request = build_apns_for_job(
        payload=payload,
        channel="system",
        token_value="tok",
        bundle_id="org.ntust.app.TigerDuck",
    )
    alert = request.message["aps"]["alert"]
    assert alert["title"].strip()
    assert alert["body"].strip()
    assert request.message["kind"] == "reauth_required"


def test_fcm_data_is_not_empty():
    payload = build_reauth_payload(provider="moodle", locale="zh-Hant")
    request = build_fcm_for_job(
        payload=payload, channel="system", token_value="tok"
    )
    assert request.title.strip()
    assert request.body.strip()
    assert request.data["kind"] == "reauth_required"
    assert request.data["android_channel_id"] == "system"


def test_scenario_name_unchanged():
    # The dedupe key embeds it; changing it would resurrect settled jobs.
    assert REAUTH_SCENARIO == "reauth_required"


# --- Delivery conditions (spec §4.5) ----------------------------------------


class ScriptedSender:
    """PushSender double recording every request it is handed.

    Duplicated from `test_push_pipeline.py` rather than shared: every push
    test module in this repo keeps its own transport double, and a shared
    one would couple two suites that assert on different things.
    """

    def __init__(self):
        self.requests = []

    async def send(self, request):
        self.requests.append(request)
        return SendResult(success=True, status="200")

    async def send_multi(self, requests):
        return [await self.send(r) for r in requests]

    async def close(self):
        pass


def _worker(prepared_engine, test_settings, apple, android):
    return PushPipelineWorker(
        session_factory=build_session_factory(prepared_engine),
        settings=test_settings,
        router=PushRouter(apple=apple, android=android),
        worker_id="reauth-test-worker",
    )


async def _account_with_devices(session, devices):
    """A user + ExternalAccount, plus one UserDevice per spec in `devices`.

    Each entry is a dict of UserDevice kwargs; `with_token` (popped) adds an
    active standard push token so the device can appear in a delivery round.
    Returns (user, account, {client_device_id: UserDevice}).
    """
    user = User(student_id="b11203058")
    session.add(user)
    await session.flush()
    account = ExternalAccount(
        user_id=user.id, provider="ntust_sso", external_user_id="b11203058"
    )
    session.add(account)
    await session.flush()

    made: dict[str, UserDevice] = {}
    for spec in devices:
        spec = dict(spec)
        token_provider = spec.pop("with_token", None)
        device = UserDevice(user_id=user.id, **spec)
        session.add(device)
        await session.flush()
        made[device.client_device_id] = device
        if token_provider:
            session.add(
                DevicePushToken(
                    device_id=device.id,
                    provider=token_provider,
                    token_kind="standard",
                    token_hash=f"hash-{device.client_device_id}",
                    token_value=f"tok-{device.client_device_id}",
                    bundle_id="org.ntust.app.TigerDuck",
                )
            )
    await session.commit()
    return user, account, made


async def _invalidate(session, *, account, user):
    await mark_credentials_invalid(
        session, account=account, user_id=user.id, error="credential_invalid"
    )
    await session.commit()


async def _jobs_for(session, user):
    return (
        (await session.execute(select(PushJob).where(PushJob.user_id == user.id)))
        .scalars()
        .all()
    )


@asyncio_session
@pytest.mark.parametrize(
    ("label", "device"),
    [
        ("android only", {"client_device_id": "pixel", "platform": "android"}),
        ("macos only", {"client_device_id": "mbp", "platform": "macos"}),
        (
            "ios with course sync off",
            {
                "client_device_id": "iphone",
                "platform": "ios",
                "cloud_sync_enabled": False,
            },
        ),
        (
            "soft-deleted ios",
            {
                "client_device_id": "iphone",
                "platform": "ios",
                "deleted_at": datetime(2020, 1, 1, tzinfo=UTC),
            },
        ),
    ],
)
async def test_no_job_without_a_device_that_can_reauth(db_session, label, device):
    user, account, _ = await _account_with_devices(db_session, [device])
    await _invalidate(db_session, account=account, user=user)
    assert await _jobs_for(db_session, user) == [], label


@asyncio_session
async def test_no_job_when_the_account_has_no_devices_at_all(db_session):
    user, account, _ = await _account_with_devices(db_session, [])
    await _invalidate(db_session, account=account, user=user)
    assert await _jobs_for(db_session, user) == []


@asyncio_session
@pytest.mark.parametrize("platform", ["ios", "ipados"])
async def test_job_queued_for_an_iphone_or_ipad_with_sync_on(db_session, platform):
    user, account, _ = await _account_with_devices(
        db_session, [{"client_device_id": "dev", "platform": platform}]
    )
    await _invalidate(db_session, account=account, user=user)

    job = (await _jobs_for(db_session, user))[0]
    assert job.scenario == REAUTH_SCENARIO
    assert job.channel == "system"
    # Language-independent: the copy is resolved per recipient at send time,
    # so a job that carried a title would have baked in one device's language.
    assert job.payload == {"kind": REAUTH_SCENARIO, "provider": "ntust_sso"}
    assert "title" not in job.payload


@asyncio_session
async def test_macos_never_receives_the_reauth_push(
    db_session, prepared_engine, test_settings
):
    user, account, made = await _account_with_devices(
        db_session,
        [
            {
                "client_device_id": "iphone",
                "platform": "ios",
                "locale": "en",
                "with_token": "apns",
            },
            {
                "client_device_id": "mbp",
                "platform": "macos",
                "locale": "en",
                "with_token": "apns",
            },
        ],
    )
    await _invalidate(db_session, account=account, user=user)
    await _run_tick(db_session, user, prepared_engine, test_settings)

    delivered_to = {
        row.device_id
        for row in (await db_session.execute(select(PushDelivery))).scalars()
    }
    assert delivered_to == {made["iphone"].id}


@asyncio_session
async def test_copy_is_resolved_from_each_recipient_device_locale(
    db_session, prepared_engine, test_settings
):
    user, account, _ = await _account_with_devices(
        db_session,
        [
            {
                "client_device_id": "iphone-tw",
                "platform": "ios",
                "locale": "zh-Hant-TW",
                "with_token": "apns",
            },
            {
                "client_device_id": "ipad-jp",
                "platform": "ipados",
                "locale": "ja-JP",
                "with_token": "apns",
            },
            {
                # Registered before the client shipped `locale` — falls back
                # to English rather than sending an empty banner.
                "client_device_id": "pixel",
                "platform": "android",
                "locale": None,
                "with_token": "fcm",
            },
        ],
    )
    await _invalidate(db_session, account=account, user=user)
    apple, android = await _run_tick(
        db_session, user, prepared_engine, test_settings
    )

    key = "notification_reauth_required_title"
    apns_titles = {r.message["aps"]["alert"]["title"] for r in apple.requests}
    assert apns_titles == {translate(key, "zh-Hant"), translate(key, "ja")}
    assert len(apns_titles) == 2

    assert [r.title for r in android.requests] == [translate(key, "en")]
    assert android.requests[0].data["kind"] == REAUTH_SCENARIO
    assert android.requests[0].data["android_channel_id"] == "system"


@asyncio_session
async def test_empty_copy_is_skipped_loudly_instead_of_sent(
    db_session, prepared_engine, test_settings, monkeypatch
):
    """A copy regression must surface on the server, not vanish on the device.

    Android's `FcmService` drops a `reauth_required` with no title/body and
    only logs; APNs delivers a banner that rings with nothing written on it.
    Both are the failure mode least likely to be reported, so the pipeline
    refuses to send rather than producing either.
    """
    user, account, _ = await _account_with_devices(
        db_session,
        [
            {
                "client_device_id": "iphone",
                "platform": "ios",
                "locale": "en",
                "with_token": "apns",
            }
        ],
    )
    await _invalidate(db_session, account=account, user=user)

    monkeypatch.setattr(
        "server.push.pipeline.build_reauth_payload",
        lambda **_: {"kind": REAUTH_SCENARIO, "provider": "x", "title": "", "body": ""},
    )
    apple, _android = await _run_tick(
        db_session, user, prepared_engine, test_settings
    )

    assert apple.requests == []
    delivery = (await db_session.execute(select(PushDelivery))).scalar_one()
    assert delivery.status == "skipped"
    assert delivery.failure_code == "empty_copy"
    job = (await _jobs_for(db_session, user))[0]
    # Every delivery skipped — the job settles terminally with a code an
    # operator can search for, rather than reporting a push nobody received.
    assert job.status == "failed"
    assert job.last_error == "all_tokens_skipped"


async def _run_tick(session, user, prepared_engine, test_settings):
    """Backdate the queued job, run one push tick, return the two senders.

    `mark_credentials_invalid` fires the job at `now`, and `available_at`
    defaults to the INSERT transaction timestamp — either can land after the
    tick's claim cutoff. `test_push_pipeline.py` backdates for the same
    reason; a single-tick test cannot self-heal from a missed claim.
    """
    past = datetime.now(UTC) - timedelta(minutes=5)
    job = (await _jobs_for(session, user))[0]
    job.fire_at = past
    job.available_at = past
    await session.commit()

    apple, android = ScriptedSender(), ScriptedSender()
    await run_push_tick(_worker(prepared_engine, test_settings, apple, android))
    return apple, android
