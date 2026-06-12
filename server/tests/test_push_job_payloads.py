"""push_jobs.payload → transport request mapping."""

from __future__ import annotations

from datetime import UTC, datetime

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
