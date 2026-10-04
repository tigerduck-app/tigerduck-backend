"""The portal's custom push, checked against the server code that sends it.

The portal writes these push_jobs rows with raw SQL and does not import the
server package, so nothing else keeps the payload in the shape the push
pipeline and the two apps expect. Lives in the server suite for the reason
test_calendar_gaps.py gives: the portal has no test runner of its own.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from portal.app.routes.custom_push import build_custom_push_payload  # noqa: E402
from server.push.job_payloads import build_apns_for_job, build_fcm_for_job  # noqa: E402

_NOW = datetime(2026, 10, 4, 2, 0, tzinfo=UTC)
_NOTIFICATION_ID = "ecc66bcb4a75f302"


def _popup(*, force_ring: bool = False) -> dict:
    return build_custom_push_payload(
        title="停課通知",
        body="明天停課",
        force_ring=force_ring,
        notification_id=_NOTIFICATION_ID,
    )


def _record(*, force_ring: bool = False) -> dict:
    return build_custom_push_payload(
        title="停課通知",
        body="明天停課",
        force_ring=force_ring,
        bulletin_id=77,
    )


def _fcm_data(payload: dict) -> dict:
    return build_fcm_for_job(
        payload=payload, channel="custom", token_value="fcm-tok"
    ).data


def _apns_message(payload: dict) -> dict:
    return build_apns_for_job(
        payload=payload,
        channel="custom",
        token_value="ab" * 32,
        bundle_id="org.ntust.app.TigerDuck",
        now=_NOW,
    ).message


def test_a_popup_reaches_android_as_a_popup_it_shows() -> None:
    # FcmService shows a custom_push_popup only when it carries a
    # notification_id; a message of any other kind without a bulletin_id is
    # dropped without a word.
    data = _fcm_data(_popup())

    assert data["kind"] == "custom_push_popup"
    assert data["notification_id"] == _NOTIFICATION_ID
    assert (data["title"], data["body"]) == ("停課通知", "明天停課")


def test_a_popup_on_an_iphone_carries_what_the_tap_handler_reads() -> None:
    # TigerDuckApp.routeServerPushTap opens the in-app popup only when
    # kind, notification_id, title and body are all top-level keys.
    message = _apns_message(_popup())

    assert message["kind"] == "custom_push_popup"
    assert message["notification_id"] == _NOTIFICATION_ID
    assert (message["title"], message["body"]) == ("停課通知", "明天停課")
    assert message["aps"]["alert"] == {"title": "停課通知", "body": "明天停課"}


def test_a_recorded_push_points_both_apps_at_its_bulletin() -> None:
    data = _fcm_data(_record())
    message = _apns_message(_record())

    assert data["kind"] == "custom_push_bulletin"
    assert data["bulletin_id"] == "77"
    assert message["kind"] == "custom_push_bulletin"
    assert int(message["bulletin_id"]) == 77


@pytest.mark.parametrize("build", [_popup, _record], ids=["popup", "record"])
@pytest.mark.parametrize("force_ring", [False, True])
def test_an_iphone_rings_only_when_the_operator_asked(build, force_ring) -> None:
    aps = _apns_message(build(force_ring=force_ring))["aps"]

    assert ("sound" in aps) is force_ring
