"""The portal's test Live Activity start, checked against the server code
that sends it.

The portal writes that push_jobs row with raw SQL and does not import the
server package, so nothing else keeps its payload in the shape the push
pipeline and the app expect. Lives in the server suite for the reason
test_calendar_gaps.py gives: the portal has no test runner of its own.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from portal.app.routes.moodle.manual_tests import (  # noqa: E402
    build_live_activity_payload,
)
from server.push.dedupe import SCHEDULE_CHANNEL  # noqa: E402
from server.push.job_payloads import build_apns_for_job  # noqa: E402

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
