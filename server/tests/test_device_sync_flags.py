"""The two device-level reminder switches added in v2.1.0.

Both default to true: an upgrading user who has never seen the new
settings row must not silently lose reminders because a column appeared.

Fixture note: this suite follows `test_user_devices_v3.py` / `test_device_locale.py`
(the real /v3 device test files — there is no `test_user_devices.py` and no
`registered_device` fixture in this repo). Auth headers come from a real
login via the local `do_login`/`bearer` helpers, duplicated per-file the
same way every other /v3 test module does it.

There is also no GET .../preferences route — only the PATCH handler at
`/v3/devices/{device_id}/preferences`. Every field on the request is
optional, so an empty-body PATCH (`json={}`) changes nothing and its
response reflects the device's current state, which doubles as a read.

Applied per-function below (not as a module-level `pytestmark`) because
this file also has a plain sync test (`test_model_columns_are_nonnull_with_true_default`)
that touches no fixture and needs no event loop; pytest-asyncio warns if
the asyncio mark reaches a non-async function.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from server.auth.models import UserDevice
from server.auth.moodle import MoodleVerifyResult, StaticMoodleVerifier
from server.db import build_session_factory

asyncio_session = pytest.mark.asyncio(loop_scope="session")


def login_body(student_id: str = "B11015000", device: str = "iphone-abc") -> dict:
    return {
        "student_id": student_id,
        "password": "pw",
        "moodle_token": "tok",
        "device_info": {"client_device_id": device, "platform": "ios"},
    }


async def do_login(client, **kwargs) -> dict:
    client.app.state.moodle_verifier = StaticMoodleVerifier(
        MoodleVerifyResult(ok=True, username="whatever")
    )
    response = await client.post("/v3/auth/login", json=login_body(**kwargs))
    assert response.status_code == 200
    return response.json()


def bearer(login: dict) -> dict:
    return {"Authorization": f"Bearer {login['access_token']}"}


async def _device_flags(client, client_device_id: str) -> tuple[bool, bool]:
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        device = (
            await session.execute(
                select(UserDevice).where(
                    UserDevice.client_device_id == client_device_id
                )
            )
        ).scalar_one()
        return device.sync_assignment_reminders, device.sync_live_activity


@asyncio_session
async def test_new_flags_default_to_true(client) -> None:
    login = await do_login(client)
    response = await client.patch(
        "/v3/devices/iphone-abc/preferences",
        headers=bearer(login),
        json={},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["sync_assignment_reminders"] is True
    assert body["sync_live_activity"] is True
    assert await _device_flags(client, "iphone-abc") == (True, True)


@asyncio_session
async def test_flags_round_trip_through_preferences_patch(client) -> None:
    login = await do_login(client, device="iphone-flags")
    response = await client.patch(
        "/v3/devices/iphone-flags/preferences",
        headers=bearer(login),
        json={"sync_assignment_reminders": False, "sync_live_activity": False},
    )
    assert response.status_code == 200, response.text
    assert response.json()["sync_assignment_reminders"] is False
    assert response.json()["sync_live_activity"] is False

    # No GET route: re-read via an empty-body PATCH.
    again = await client.patch(
        "/v3/devices/iphone-flags/preferences",
        headers=bearer(login),
        json={},
    )
    assert again.status_code == 200, again.text
    assert again.json()["sync_assignment_reminders"] is False
    assert again.json()["sync_live_activity"] is False
    assert await _device_flags(client, "iphone-flags") == (False, False)


@asyncio_session
async def test_omitting_a_flag_leaves_the_other_unchanged(client) -> None:
    login = await do_login(client, device="iphone-partial")
    await client.patch(
        "/v3/devices/iphone-partial/preferences",
        headers=bearer(login),
        json={"sync_assignment_reminders": False},
    )
    # A PATCH that mentions only one flag (or an unrelated one) must not
    # reset the other back to its default.
    body = (
        await client.patch(
            "/v3/devices/iphone-partial/preferences",
            headers=bearer(login),
            json={"sync_courses": False},
        )
    ).json()
    assert body["sync_assignment_reminders"] is False
    assert body["sync_live_activity"] is True


def test_model_columns_are_nonnull_with_true_default() -> None:
    """Backward-compatibility guard: both columns must be NOT NULL with a
    `server_default` of true, so a pre-migration row — or a PATCH body from
    an old client that never sends these fields — resolves to True, never
    NULL."""
    table = UserDevice.__table__
    for name in ("sync_assignment_reminders", "sync_live_activity"):
        column = table.c[name]
        assert column.nullable is False, name
        assert column.server_default is not None, name
        assert column.server_default.arg.text == "true", name
        assert column.default.arg is True, name
