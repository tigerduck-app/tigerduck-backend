"""Unit tests for push job payload building."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from server.push.job_payloads import build_apns_for_job
from server.push.payload import PushKind


def _start_payload():
    """The job payload as `/schedule/sync` writes it: the client's snapshot
    flattened together with the routing keys, `activity_id` composed the
    way the client composes `LiveActivitySnapshot.composedActivityId`."""
    return {
        "kind": "schedule",
        "scenario": "inClass",
        "source_id": "c1",
        "activity_id": "inClass::c1",
        "title": "Math",
        "subtitle": "09:10-10:00",
        "accentHex": 16743680,
        "sourceId": "c1",
        "countdownTarget": "2026-09-09T02:00:00+00:00",
    }


# A schedule job starts an activity that is not running yet. ActivityKit
# will only do that for a push that carries everything `Activity.request`
# would — the attributes type and value, an alert — and it drops a start it
# cannot decode as silently as it drops an undecodable end. These pin the
# shape against Apple's push-to-start contract and the client's attributes.


def test_schedule_start_is_a_push_to_start():
    req = build_apns_for_job(
        payload=_start_payload(),
        channel="schedule",
        token_value="abc",
        bundle_id="org.ntust.app.TigerDuck",
    )
    aps = req.message["aps"]
    assert req.kind == PushKind.live_activity
    assert req.topic == "org.ntust.app.TigerDuck.push-type.liveactivity"
    assert aps["event"] == "start"
    # `TigerDuckActivityAttributes` has exactly one stored property.
    assert aps["attributes-type"] == "TigerDuckActivityAttributes"
    assert aps["attributes"] == {"activityId": "inClass::c1"}
    assert set(aps["content-state"]) == {"snapshot"}
    assert aps["content-state"]["snapshot"]["scenario"] == "inClass"
    # Apple requires an alert on a start so the person is told why
    # something appeared.
    assert aps["alert"] == {"title": "Math", "body": "09:10-10:00"}
    assert "dismissal-date" not in aps


def test_schedule_start_stale_date_is_the_countdown_in_unix_seconds():
    req = build_apns_for_job(
        payload=_start_payload(),
        channel="schedule",
        token_value="abc",
        bundle_id="org.ntust.app.TigerDuck",
    )
    aps = req.message["aps"]
    expected = int(datetime(2026, 9, 9, 2, tzinfo=UTC).timestamp())
    # `aps` dates are Unix seconds; only the content-state uses Swift's
    # reference date.
    assert aps["stale-date"] == expected
    assert aps["content-state"]["snapshot"]["countdownTarget"] != expected


def test_schedule_start_without_countdown_has_no_stale_date():
    payload = _start_payload()
    del payload["countdownTarget"]
    req = build_apns_for_job(
        payload=payload,
        channel="schedule",
        token_value="abc",
        bundle_id="org.ntust.app.TigerDuck",
    )
    assert "stale-date" not in req.message["aps"]


def test_schedule_start_names_the_attributes_type_the_token_registered():
    # The client files its push-to-start token under the attributes type
    # name; if that ever changes, the payload must follow the token, not
    # a constant here.
    req = build_apns_for_job(
        payload=_start_payload(),
        channel="schedule",
        token_value="abc",
        bundle_id="org.ntust.app.TigerDuck",
        attributes_type="RenamedAttributes",
    )
    assert req.message["aps"]["attributes-type"] == "RenamedAttributes"


def test_schedule_start_refuses_a_payload_without_an_activity_id():
    payload = _start_payload()
    del payload["activity_id"]
    with pytest.raises(ValueError):
        build_apns_for_job(
            payload=payload,
            channel="schedule",
            token_value="abc",
            bundle_id="org.ntust.app.TigerDuck",
        )


def test_live_activity_pushes_collapse_per_activity():
    """APNs keeps one undelivered push per collapse id. One id for the whole
    channel let class B's start evict class A's while the phone slept; per
    activity, only a later push for the same activity replaces an earlier
    one — an end overtaking an undelivered start is right, the class is
    over."""
    start = build_apns_for_job(
        payload=_start_payload(),
        channel="schedule",
        token_value="abc",
        bundle_id="org.ntust.app.TigerDuck",
    )
    end = build_apns_for_job(
        payload={**_end_payload(), "activity_id": "inClass::c1"},
        channel="schedule",
        token_value="abc",
        bundle_id="org.ntust.app.TigerDuck",
    )
    other = build_apns_for_job(
        payload={**_start_payload(), "activity_id": "inClass::c2"},
        channel="schedule",
        token_value="abc",
        bundle_id="org.ntust.app.TigerDuck",
    )
    assert start.collapse_id == end.collapse_id
    assert start.collapse_id != other.collapse_id
    assert len(start.collapse_id.encode()) <= 64


def test_schedule_start_drops_routing_keys_from_the_snapshot():
    req = build_apns_for_job(
        payload=_start_payload(),
        channel="schedule",
        token_value="abc",
        bundle_id="org.ntust.app.TigerDuck",
    )
    snapshot = req.message["aps"]["content-state"]["snapshot"]
    for routing_key in ("kind", "activity_id", "source_id"):
        assert routing_key not in snapshot


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
    # Only a start instantiates attributes; an end addresses a running
    # activity through its own token.
    assert "attributes-type" not in req.message["aps"]
    assert "attributes" not in req.message["aps"]
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
