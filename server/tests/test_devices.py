"""Device tests for the surfaces that survived the v2 sunset.

The anonymous /v2/devices HTTP endpoints were retired (middleware answers
410 Gone); the user-scoped replacement lives at /v3/devices and is covered
end-to-end in test_user_devices_v3.py. This file keeps what is still live:
the /v3 device-preferences PATCH contract and the `device_registrations`
model columns the push pipeline still reads.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from server.auth.moodle import MoodleVerifyResult, StaticMoodleVerifier
from server.models import DeviceRegistration

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _login_headers(client: AsyncClient, device_id: str) -> dict:
    """Log the test user in on `device_id` and return Bearer headers.

    /v3/auth/login upserts the UserDevice row, so no separate register
    call is needed before hitting /v3/devices/{device_id}/preferences.
    """
    client.app.state.moodle_verifier = StaticMoodleVerifier(
        MoodleVerifyResult(ok=True, username="whatever")
    )
    response = await client.post(
        "/v3/auth/login",
        json={
            "student_id": "B11015000",
            "password": "pw",
            "moodle_token": "tok",
            "device_info": {"client_device_id": device_id, "platform": "ios"},
        },
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def test_device_registration_persists_class_and_opt_in(db_session):
    # The anonymous device_registrations table is still read by the push
    # pipeline (custom_push_targeting filters on server_push_enabled and
    # device_class), so the model mapping must keep persisting both columns.
    db_session.add(
        DeviceRegistration(
            device_id="iphone-d1",
            user_id="user-abc",
            pts_token_hex="a1b2c3" * 10,
            bundle_id="org.ntust.app.TigerDuck",
            attrs_type="TigerDuckActivityAttributes",
            apns_env="development",
            device_class="iphone",
            server_push_enabled=True,
        )
    )
    await db_session.commit()
    db_session.expire_all()

    row = (
        await db_session.execute(
            select(DeviceRegistration).where(
                DeviceRegistration.device_id == "iphone-d1"
            )
        )
    ).scalar_one()
    assert row.device_class == "iphone"
    assert row.server_push_enabled is True


async def test_patch_preferences_flips_server_push_enabled(client: AsyncClient):
    headers = await _login_headers(client, "iphone-d2")
    resp = await client.patch(
        "/v3/devices/iphone-d2/preferences",
        headers=headers,
        json={"server_push_enabled": False},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "device_id": "iphone-d2",
        "server_push_enabled": False,
        "sync_courses": True,
        "sync_course_colors": True,
        "sync_course_names": True,
        "sync_assignments": True,
        "cloud_sync_enabled": True,
    }


async def test_patch_preferences_404_for_unknown_device(client: AsyncClient):
    headers = await _login_headers(client, "iphone-d3")
    resp = await client.patch(
        "/v3/devices/does-not-exist/preferences",
        headers=headers,
        json={"server_push_enabled": False},
    )
    assert resp.status_code == 404
