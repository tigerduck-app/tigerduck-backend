"""The portal's test Live Activity start, checked against the server code
that sends it.

The portal writes that push_jobs row with raw SQL and does not import the
server package, so nothing else keeps its payload in the shape the push
pipeline and the app expect. Lives in the server suite, the only one
with the server code and a database.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest  # noqa: E402

from portal.app.routes.moodle.manual_tests import (  # noqa: E402
    build_live_activity_payload,
)
from server.auth.crypto import CredentialCipher  # noqa: E402
from server.auth.models import (  # noqa: E402
    DevicePushToken,
    PushJob,
    User,
    UserDevice,
)
from server.db import build_session_factory  # noqa: E402
from server.push.apns_client import SendResult  # noqa: E402
from server.push.dedupe import SCHEDULE_CHANNEL  # noqa: E402
from server.push.job_payloads import build_apns_for_job  # noqa: E402
from server.push.pipeline import PushPipelineWorker, run_push_tick  # noqa: E402
from server.push.router import PushRouter  # noqa: E402
from server.tests.conftest import SubmissionStatusStub  # noqa: E402

_NOW = datetime(2026, 9, 14, 2, 15, tzinfo=UTC)
# Seconds from the Unix epoch to Swift's reference date, 2001-01-01.
_SWIFT_REFERENCE_EPOCH = 978307200
_SOURCE_ID = "portal-test-abcd1234"


def _payload(**overrides) -> dict:
    args = {
        "scenario": "inClass",
        "title": "Portal test",
        "subtitle": "Live Activity test",
        "location": "TR-313",
        "minutes": 2,
        "now": _NOW,
        "source_id": _SOURCE_ID,
    }
    args.update(overrides)
    return build_live_activity_payload(**args)


def _start_aps(payload: dict) -> dict:
    request = build_apns_for_job(
        payload=payload,
        channel=SCHEDULE_CHANNEL,
        token_value="ab" * 32,
        bundle_id="org.ntust.app.TigerDuck",
        now=_NOW,
    )
    return request.message["aps"]


def test_the_start_push_carries_a_snapshot_the_app_can_decode() -> None:
    aps = _start_aps(_payload())

    assert aps["event"] == "start"
    assert aps["attributes"] == {"activityId": f"inClass::{_SOURCE_ID}"}
    assert aps["alert"] == {"title": "Portal test", "body": "Live Activity test"}
    snapshot = aps["content-state"]["snapshot"]
    # Every non-optional LiveActivitySnapshot field is there, and none of the
    # pipeline's routing keys leaked into the content state.
    assert {"scenario", "title", "subtitle", "accentHex", "sourceId"} <= snapshot.keys()
    assert not {"kind", "activity_id", "source_id"} & snapshot.keys()
    target = _NOW + timedelta(minutes=2)
    assert snapshot["countdownTarget"] == target.timestamp() - _SWIFT_REFERENCE_EPOCH
    assert snapshot["progressStart"] == _NOW.timestamp() - _SWIFT_REFERENCE_EPOCH
    assert aps["stale-date"] == int(target.timestamp())


def test_the_activity_id_is_the_one_the_register_endpoint_accepts() -> None:
    # `/live-activities/register` refuses an id that is not
    # "{snapshot.scenario}::{source_id}", and the phone sends the snapshot's
    # own sourceId as source_id. Anything else and the end is never filed.
    for scenario in ("inClass", "classPreparing", "assignmentUrgent"):
        payload = _payload(scenario=scenario)
        assert payload["activity_id"] == f"{payload['scenario']}::{payload['sourceId']}"
        assert payload["source_id"] == payload["sourceId"]


def test_a_class_yet_to_start_has_no_progress_bar_and_no_location_is_null() -> None:
    payload = _payload(scenario="classPreparing", location="")

    assert payload["progressStart"] is None
    assert payload["locationText"] is None


class _Sender:
    """PushSender double that always succeeds, as in every push test module."""

    def __init__(self):
        self.requests = []

    async def send(self, request):
        self.requests.append(request)
        return SendResult(success=True, status="200")

    async def send_multi(self, requests):
        return [await self.send(r) for r in requests]

    async def close(self):
        pass


@pytest.mark.asyncio(loop_scope="session")
async def test_an_assignment_test_start_is_sent_without_the_pre_send_check(
    db_session, prepared_engine, test_settings
):
    """The pre-send check looks up the assignment an assignmentUrgent start
    counts down to. The portal's start names none, under a key of its own,
    and must still go out rather than be cancelled as stale."""
    user = User(student_id="b11203058")
    db_session.add(user)
    await db_session.flush()
    device = UserDevice(user_id=user.id, client_device_id="dev-1", platform="ios")
    db_session.add(device)
    await db_session.flush()
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
    now = datetime.now(UTC)
    # As `send_test_live_activity` inserts it.
    job = PushJob(
        user_id=user.id,
        device_id=device.id,
        dedupe_key=f"portal_la:{device.id}:{_SOURCE_ID}",
        channel=SCHEDULE_CHANNEL,
        scenario="assignmentUrgent",
        fire_at=now - timedelta(seconds=1),
        available_at=now - timedelta(seconds=1),
        payload=_payload(scenario="assignmentUrgent", now=now),
    )
    db_session.add(job)
    await db_session.commit()

    moodle = SubmissionStatusStub()
    apple = _Sender()
    await run_push_tick(
        PushPipelineWorker(
            session_factory=build_session_factory(prepared_engine),
            settings=test_settings,
            router=PushRouter(apple=apple, android=_Sender()),
            worker_id="portal-test-worker",
            cipher=CredentialCipher.from_settings(test_settings),
            fetcher=moodle,
        )
    )

    await db_session.refresh(job)
    assert job.status == "sent"
    assert moodle.asked == []
    assert [r.device_token for r in apple.requests] == ["pts-tok"]
