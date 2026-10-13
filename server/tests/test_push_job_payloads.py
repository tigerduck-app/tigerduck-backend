"""push_jobs.payload → transport request mapping."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from server.push.job_payloads import build_apns_for_job, build_fcm_for_job
from server.push.payload import PushKind

PAYLOAD = {
    "kind": "assignment_reminder",
    "title": "作業提醒：HW3",
    "body": "資料結構 · 剩 24 小時",
    "moodle_assignment_id": 555,
    "moodle_course_id": 7001,
    "due_at": "2026-06-12T15:00:00+00:00",
    "moodle_url": "https://moodle.example.edu/mod/assign/view.php?id=9001",
}
NOW = datetime(2026, 6, 11, 15, 0, tzinfo=UTC)


def test_apns_request_shape():
    request = build_apns_for_job(
        payload=PAYLOAD,
        channel="assignment",
        token_value="tok-abc",
        bundle_id="org.ntust.app.TigerDuck",
        now=NOW,
    )
    assert request.kind is PushKind.alert
    assert request.device_token == "tok-abc"
    assert request.topic == "org.ntust.app.TigerDuck"
    aps = request.message["aps"]
    assert aps["alert"] == {"title": PAYLOAD["title"], "body": PAYLOAD["body"]}
    assert aps["thread-id"] == "assignment"
    assert request.message["kind"] == "assignment_reminder"
    assert request.message["moodle_assignment_id"] == "555"
    assert request.message["due_at"] == PAYLOAD["due_at"]
    assert request.expiration > int(NOW.timestamp())


@pytest.mark.parametrize(
    ("due_in", "held_for"),
    [
        (timedelta(hours=2), timedelta(hours=2)),
        (timedelta(days=3), timedelta(hours=24)),
    ],
    ids=["due-within-a-day", "due-later"],
)
def test_an_assignment_reminder_is_held_until_its_due_time_at_most(due_in, held_for):
    # A phone offline until after the deadline must not show "due in 2
    # hours" when it comes back; otherwise the usual day applies.
    request = build_apns_for_job(
        payload={**PAYLOAD, "due_epoch": int((NOW + due_in).timestamp())},
        channel="assignment",
        token_value="tok-abc",
        bundle_id="org.ntust.app.TigerDuck",
        now=NOW,
    )
    assert request.expiration == int((NOW + held_for).timestamp())


def test_only_an_assignment_reminder_is_held_to_a_due_time():
    request = build_apns_for_job(
        payload={**PAYLOAD, "due_epoch": int((NOW + timedelta(hours=2)).timestamp())},
        channel="course",
        token_value="tok-abc",
        bundle_id="org.ntust.app.TigerDuck",
        now=NOW,
    )
    assert request.expiration == int((NOW + timedelta(hours=24)).timestamp())


@pytest.mark.parametrize(
    ("channel", "thread"),
    [
        ("course", "course"),
        ("assignment", "assignment"),
        ("bulletin", "other"),
        ("custom", "other"),
        ("system", "other"),
    ],
)
def test_alert_stacks_with_its_kind(channel, thread):
    # Class and homework reminders stack apart; everything else shares one.
    request = build_apns_for_job(
        payload=PAYLOAD,
        channel=channel,
        token_value="tok-abc",
        bundle_id="org.ntust.app.TigerDuck",
        now=NOW,
    )
    assert request.message["aps"]["thread-id"] == thread


@pytest.mark.parametrize("channel", ["course", "assignment"])
def test_reminders_do_not_replace_one_another(channel):
    # A collapse id would make each reminder replace the one on screen.
    request = build_apns_for_job(
        payload=PAYLOAD,
        channel=channel,
        token_value="tok-abc",
        bundle_id="org.ntust.app.TigerDuck",
        now=NOW,
    )
    assert request.collapse_id is None


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "custom_push_bulletin", "bulletin_id": 7, "title": "t", "body": "b"},
        {"kind": "custom_push_popup", "notification_id": "n-1", "title": "t", "body": "b"},
    ],
)
def test_custom_pushes_stack_with_other(payload):
    # The portal's pushes go through their own builders, not the standard
    # alert, and land in the same stack as everything else.
    request = build_apns_for_job(
        payload=payload,
        channel="custom",
        token_value="tok-abc",
        bundle_id="org.ntust.app.TigerDuck",
        now=NOW,
    )
    assert request.message["aps"]["thread-id"] == "other"


def test_fcm_request_shape():
    request = build_fcm_for_job(
        payload=PAYLOAD, channel="assignment", token_value="fcm-tok"
    )
    assert request.token == "fcm-tok"
    assert request.title == PAYLOAD["title"]
    assert request.data["kind"] == "assignment_reminder"
    assert request.data["moodle_assignment_id"] == "555"
    assert request.data["android_channel_id"] == "assignments"
    assert all(isinstance(v, str) for v in request.data.values())


def test_missing_title_falls_back_to_empty():
    request = build_fcm_for_job(payload={}, channel="system", token_value="t")
    assert request.title == ""
    assert request.data["android_channel_id"] == "system"
