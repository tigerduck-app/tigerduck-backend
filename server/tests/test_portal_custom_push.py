"""The portal's custom push, checked against the server code that sends it.

The portal writes these push_jobs rows with raw SQL and does not import the
server package, so nothing else keeps the payload in the shape the push
pipeline and the two apps expect. Lives in the server suite for the reason
test_calendar_gaps.py gives: the portal has no test runner of its own.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from portal.app.db import _asyncpg_dsn  # noqa: E402
from portal.app.routes.custom_push import (  # noqa: E402
    _PreviewRequest,
    build_custom_push_payload,
    preview,
)
from server.auth.models import DevicePushToken, User, UserDevice  # noqa: E402
from server.models import DeviceRegistration  # noqa: E402
from server.push.custom_push_targeting import TargetFilter, count_by_class  # noqa: E402
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


@pytest.mark.parametrize("build", [_popup, _record], ids=["popup", "record"])
@pytest.mark.parametrize("force_ring", [False, True])
def test_android_is_told_to_ring_only_when_the_operator_asked(build, force_ring) -> None:
    # FcmService picks the audible or silent channel from force_ring; the
    # channel id alongside it has to say the same thing.
    data = _fcm_data(build(force_ring=force_ring))

    assert data["force_ring"] == ("true" if force_ring else "false")
    assert data["android_channel_id"] == (
        "bulletins_sound" if force_ring else "bulletins_silent"
    )


@pytest_asyncio.fixture(loop_scope="session")
async def portal_pool(test_settings, db_session):
    pool = await asyncpg.create_pool(
        _asyncpg_dsn(test_settings.database_url), min_size=1, max_size=2
    )
    yield pool
    await pool.close()


async def _recipients(db_session) -> None:
    """A signed-in iPhone, and anonymous devices a recorded push's filter
    takes (an iPhone, an Apple row from before device_class, a Pixel) or
    leaves out (one linked to the student, one without a token)."""
    user = User(student_id="b1")
    db_session.add(user)
    await db_session.flush()
    phone = UserDevice(
        user_id=user.id, client_device_id="iphone-1", platform="ios", device_class="iphone"
    )
    db_session.add(phone)
    await db_session.flush()
    db_session.add(
        DevicePushToken(
            device_id=phone.id,
            provider="apns",
            token_kind="standard",
            token_hash="iphone-1",
            token_value="tok",
            status="active",
        )
    )
    for device_id, platform, device_class, token, linked in (
        ("anon-iphone", "apple", "iphone", "ab", None),
        ("anon-legacy", "apple", "", "ab", None),
        ("anon-pixel", "android", "android", "fcm", None),
        ("iphone-1", "apple", "iphone", "ab", user.id),
        ("anon-no-token", "apple", "iphone", None, None),
    ):
        apple = platform == "apple"
        db_session.add(
            DeviceRegistration(
                device_id=device_id,
                user_id=f"u-{device_id}",
                platform=platform,
                device_class=device_class,
                pts_token_hex="" if apple else token,
                device_token_hex=token if apple else None,
                bundle_id="",
                attrs_type="",
                apns_env="",
                linked_user_id=linked,
            )
        )
    await db_session.commit()


async def _preview(pool, *, keeps_record: bool, **filt) -> dict:
    response = await preview(_PreviewRequest(keeps_record=keeps_record, **filt), pool=pool)
    return json.loads(response.body)["matched"]


@pytest.mark.asyncio(loop_scope="session")
async def test_the_preview_counts_the_anonymous_devices_only_a_recorded_push_reaches(
    db_session, portal_pool
):
    """tigerduck-backend#96: the bulletin dispatcher also sends a recorded
    push to the anonymous devices its filter matches; a popup goes to
    signed-in devices only."""
    await _recipients(db_session)
    classes = {"target_classes": ["iphone", "android"]}

    assert await _preview(portal_pool, keeps_record=True, **classes) == {
        "iphone": 3,
        "android": 1,
        "total": 4,
    }
    assert await _preview(portal_pool, keeps_record=False, **classes) == {
        "iphone": 1,
        "android": 0,
        "total": 1,
    }


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    "filt",
    [
        {"target_classes": ["iphone", "ipad", "mac", "android", "android_tablet"]},
        {"target_classes": ["ipad"]},
        {"target_classes": ["iphone"], "device_id": "anon-iphone"},
        {"target_classes": ["iphone", "android"], "user_id": "u-anon-pixel"},
    ],
    ids=["all", "ipad", "device", "user"],
)
async def test_the_anonymous_count_agrees_with_the_dispatcher(
    db_session, portal_pool, filt
):
    """The portal cannot import `server.push.custom_push_targeting`, which
    resolves the anonymous half of a recorded push, so it repeats its
    filter."""
    await _recipients(db_session)
    record = await _preview(portal_pool, keeps_record=True, **filt)
    popup = await _preview(portal_pool, keeps_record=False, **filt)

    anonymous = {key: count - popup.get(key, 0) for key, count in record.items()}

    assert anonymous == await count_by_class(db_session, TargetFilter(**filt))
