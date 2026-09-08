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


# The Live Activity content-state has to match the client's
# `TigerDuckActivityAttributes.ContentState`, which is a single `snapshot`
# property holding a `LiveActivitySnapshot`. ActivityKit discards a push
# whose content-state it cannot decode, so a shape mismatch here does not
# fail loudly — the activity simply never ends and the Dynamic Island
# stays up until iOS's own stale cleanup. These tests pin the shape.


def _end_payload():
    """The job payload as `/live-activities/register` writes it: the client's
    snapshot flattened together with the routing keys."""
    return {
        "kind": "live_activity_end",
        "activity_id": "inClass:c1",
        "source_id": "c1",
        # Everything below is LiveActivitySnapshot, camelCase as the client
        # encodes it (no key strategy on the Swift encoder).
        "scenario": "inClass",
        "title": "Math",
        "subtitle": "09:10-10:00",
        "locationText": "TR-101",
        "accentHex": 16743680,
        "sourceId": "c1",
        "countdownTarget": "2026-09-09T02:00:00+00:00",
    }


def test_schedule_content_state_nests_under_snapshot():
    req = build_apns_for_job(
        payload=_end_payload(),
        channel="schedule",
        token_value="abc",
        bundle_id="org.ntust.app.TigerDuck",
    )
    content_state = req.message["aps"]["content-state"]
    assert set(content_state) == {"snapshot"}, (
        "ContentState has exactly one property, `snapshot`; a flat "
        "content-state fails to decode on the client"
    )


def test_schedule_snapshot_keeps_every_non_optional_field():
    req = build_apns_for_job(
        payload=_end_payload(),
        channel="schedule",
        token_value="abc",
        bundle_id="org.ntust.app.TigerDuck",
    )
    snapshot = req.message["aps"]["content-state"]["snapshot"]
    # `scenario`, `title`, `subtitle`, `accentHex` and `sourceId` are all
    # non-optional on LiveActivitySnapshot — dropping any one of them makes
    # the whole content-state undecodable.
    assert snapshot["scenario"] == "inClass"
    assert snapshot["title"] == "Math"
    assert snapshot["subtitle"] == "09:10-10:00"
    assert snapshot["accentHex"] == 16743680
    assert snapshot["sourceId"] == "c1"


def test_schedule_snapshot_drops_routing_keys():
    req = build_apns_for_job(
        payload=_end_payload(),
        channel="schedule",
        token_value="abc",
        bundle_id="org.ntust.app.TigerDuck",
    )
    snapshot = req.message["aps"]["content-state"]["snapshot"]
    # These are ours, not the client's. Unknown keys are ignored by Swift's
    # decoder, but leaking them means the payload no longer describes what
    # it claims to.
    for routing_key in ("kind", "activity_id", "source_id"):
        assert routing_key not in snapshot


def test_schedule_snapshot_dates_are_swift_reference_seconds():
    req = build_apns_for_job(
        payload=_end_payload(),
        channel="schedule",
        token_value="abc",
        bundle_id="org.ntust.app.TigerDuck",
    )
    snapshot = req.message["aps"]["content-state"]["snapshot"]
    # Swift's default JSONDecoder reads Date as timeIntervalSinceReferenceDate,
    # so the ISO string the client sent has to come back as a number.
    assert isinstance(snapshot["countdownTarget"], (int, float))
