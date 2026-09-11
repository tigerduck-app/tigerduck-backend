"""Device locale is reported at registration and is never gated by a toggle.

Fixture note: this suite follows `test_user_devices_v3.py` (the real /v3
device test file — there is no `test_user_devices.py` and no
`registered_headers` fixture in this repo). Auth headers come from a real
login via the local `do_login`/`bearer` helpers, duplicated per-file the
same way every other /v3 test module does it.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from server.auth.models import UserDevice
from server.auth.moodle import MoodleVerifyResult, StaticMoodleVerifier
from server.db import build_session_factory

# Applied per-function below (not as a module-level `pytestmark`) because
# this file also has a plain sync test (`test_model_has_nullable_locale_column`)
# that touches no fixture and needs no event loop; pytest-asyncio warns if
# the asyncio mark reaches a non-async function.
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


async def _device_locale(client, client_device_id: str) -> str | None:
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        device = (
            await session.execute(
                select(UserDevice).where(
                    UserDevice.client_device_id == client_device_id
                )
            )
        ).scalar_one()
        return device.locale


@asyncio_session
async def test_register_stores_locale(client) -> None:
    login = await do_login(client)
    response = await client.post(
        "/v3/devices/register",
        headers=bearer(login),
        json={
            "client_device_id": "iphone-abc",
            "platform": "ios",
            "device_class": "iphone",
            "locale": "zh-Hant-TW",
        },
    )
    assert response.status_code == 200, response.text
    assert await _device_locale(client, "iphone-abc") == "zh-Hant-TW"


@asyncio_session
async def test_locale_is_optional(client) -> None:
    login = await do_login(client, device="android-1")
    response = await client.post(
        "/v3/devices/register",
        headers=bearer(login),
        json={
            "client_device_id": "android-1",
            "platform": "android",
        },
    )
    assert response.status_code == 200, response.text
    assert await _device_locale(client, "android-1") is None


@asyncio_session
async def test_reregister_updates_changed_locale(client) -> None:
    """Locale is a re-reported device fact, not a one-shot value fixed at
    first registration — a user switching the app's language must see the
    stored value change on the very next register call (every launch)."""
    login = await do_login(client, device="iphone-lang")
    body = {
        "client_device_id": "iphone-lang",
        "platform": "ios",
        "locale": "en-US",
    }
    first = await client.post(
        "/v3/devices/register", headers=bearer(login), json=body
    )
    assert first.status_code == 200, first.text
    assert await _device_locale(client, "iphone-lang") == "en-US"

    body["locale"] = "zh-Hant-TW"
    second = await client.post(
        "/v3/devices/register", headers=bearer(login), json=body
    )
    assert second.status_code == 200, second.text
    assert await _device_locale(client, "iphone-lang") == "zh-Hant-TW"


@asyncio_session
async def test_preferences_update_sets_locale(client) -> None:
    """The preferences route also accepts locale, so a device that changes
    its system language between register calls need not wait for the next
    app launch to report it."""
    login = await do_login(client, device="iphone-pref")
    await client.post(
        "/v3/devices/register",
        headers=bearer(login),
        json={"client_device_id": "iphone-pref", "platform": "ios"},
    )
    response = await client.patch(
        "/v3/devices/iphone-pref/preferences",
        headers=bearer(login),
        json={"locale": "ja-JP"},
    )
    assert response.status_code == 200, response.text
    assert await _device_locale(client, "iphone-pref") == "ja-JP"


def test_model_has_nullable_locale_column() -> None:
    column = UserDevice.__table__.c.locale
    assert column.nullable is True
