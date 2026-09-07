from server.push.payload import (
    build_alert_request,
    build_fcm_alert_request,
    build_custom_push_popup_apns,
    build_custom_push_popup_fcm,
)


def test_apns_bulletin_kind_extras_when_passed():
    req = build_alert_request(
        device_token="tok",
        bundle_id="org.ntust.app.TigerDuck",
        title="t",
        body="b",
        bulletin_id=1,
        source_url="https://x",
        canonical_org="server",
        kind="custom_push_bulletin",
        force_ring=False,
    )
    assert req.message["kind"] == "custom_push_bulletin"
    assert req.message["force_ring"] == "false"
    assert "sound" not in req.message["aps"]


def test_apns_bulletin_force_ring_sets_default_sound():
    req = build_alert_request(
        device_token="tok",
        bundle_id="org.ntust.app.TigerDuck",
        title="t",
        body="b",
        bulletin_id=1,
        source_url="https://x",
        canonical_org="server",
        kind="custom_push_bulletin",
        force_ring=True,
    )
    assert req.message["aps"]["sound"] == "default"


def test_fcm_bulletin_channel_id_silent_when_force_ring_false():
    req = build_fcm_alert_request(
        fcm_token="tok",
        title="t",
        body="b",
        bulletin_id=1,
        source_url="https://x",
        canonical_org="server",
        kind="custom_push_bulletin",
        force_ring=False,
    )
    assert req.data["kind"] == "custom_push_bulletin"
    assert req.data["force_ring"] == "false"
    assert req.data["android_channel_id"] == "bulletins_silent"


def test_fcm_bulletin_channel_id_sound_when_force_ring_true():
    req = build_fcm_alert_request(
        fcm_token="tok",
        title="t",
        body="b",
        bulletin_id=1,
        source_url="https://x",
        canonical_org="server",
        kind="custom_push_bulletin",
        force_ring=True,
    )
    assert req.data["android_channel_id"] == "bulletins_sound"


def test_fcm_bulletin_data_carries_title_and_body():
    # The Android client renders the system notification itself from
    # data-only FCM messages (so the deep-link PendingIntent gets attached);
    # `title`/`body` MUST be in `data`, not just on the FcmRequest envelope.
    req = build_fcm_alert_request(
        fcm_token="tok",
        title="hello",
        body="world",
        bulletin_id=42,
        source_url="https://x",
        canonical_org="server",
        kind="custom_push_bulletin",
        force_ring=True,
    )
    assert req.data["title"] == "hello"
    assert req.data["body"] == "world"
    assert req.data["bulletin_id"] == "42"


def test_apns_popup_payload_carries_title_body_id():
    req = build_custom_push_popup_apns(
        device_token="tok",
        bundle_id="org.ntust.app.TigerDuck",
        title="hi",
        body="hello",
        notification_id="nid-1",
        force_ring=True,
    )
    assert req.message["kind"] == "custom_push_popup"
    assert req.message["notification_id"] == "nid-1"
    assert req.message["title"] == "hi"
    assert req.message["body"] == "hello"
    assert req.message["aps"]["alert"] == {"title": "hi", "body": "hello"}
    assert req.message["aps"]["sound"] == "default"


def test_fcm_popup_payload_data_strings_only():
    req = build_custom_push_popup_fcm(
        fcm_token="tok",
        title="hi",
        body="hello",
        notification_id="nid-2",
        force_ring=False,
    )
    assert req.data == {
        "kind": "custom_push_popup",
        "title": "hi",
        "body": "hello",
        "notification_id": "nid-2",
        "force_ring": "false",
        "android_channel_id": "bulletins_silent",
    }
