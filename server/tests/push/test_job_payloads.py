"""Unit tests for push job payload building."""

from __future__ import annotations

from server.push.job_payloads import build_apns_for_job
from server.push.payload import PushKind


def test_schedule_update_payload():
    """Test that schedule channel with default kind produces an update event."""
    req = build_apns_for_job(
        payload={"kind": "schedule", "scenario": "inClass", "source_id": "c1", "title": "Math"},
        channel="schedule",
        token_value="abc",
        bundle_id="org.ntust.app.TigerDuck",
    )
    assert req.kind == PushKind.live_activity
    assert req.topic == "org.ntust.app.TigerDuck.push-type.liveactivity"
    assert req.message["aps"]["event"] == "update"
    assert "dismissal-date" not in req.message["aps"]


def test_schedule_end_payload():
    """Test that schedule channel with live_activity_end kind produces an end event with dismissal-date."""
    req = build_apns_for_job(
        payload={"kind": "live_activity_end", "scenario": "inClass", "activity_id": "x"},
        channel="schedule",
        token_value="abc",
        bundle_id="org.ntust.app.TigerDuck",
    )
    assert req.kind == PushKind.live_activity
    assert req.topic == "org.ntust.app.TigerDuck.push-type.liveactivity"
    assert req.message["aps"]["event"] == "end"
    assert "dismissal-date" in req.message["aps"]
    assert isinstance(req.message["aps"]["dismissal-date"], int)
