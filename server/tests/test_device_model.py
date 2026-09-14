"""The hardware model rides on login and on every register call.

Same fixture shape as `test_device_locale.py`: auth headers come from a
real login through the local `do_login`/`bearer` helpers.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from server.auth.models import UserDevice
from server.auth.moodle import MoodleVerifyResult, StaticMoodleVerifier
from server.db import build_session_factory

pytestmark = pytest.mark.asyncio(loop_scope="session")


def login_body(device: str, model: str | None = None) -> dict:
    info: dict = {"client_device_id": device, "platform": "android"}
    if model is not None:
        info["device_model"] = model
    return {
        "student_id": "B11015000",
        "password": "pw",
        "moodle_token": "tok",
        "device_info": info,
    }


async def do_login(client, device: str, model: str | None = None) -> dict:
    client.app.state.moodle_verifier = StaticMoodleVerifier(
        MoodleVerifyResult(ok=True, username="whatever")
    )
    response = await client.post("/v3/auth/login", json=login_body(device, model))
    assert response.status_code == 200, response.text
    return response.json()


def bearer(login: dict) -> dict:
    return {"Authorization": f"Bearer {login['access_token']}"}


async def register(client, login: dict, device: str, **extra) -> None:
    response = await client.post(
        "/v3/devices/register",
        headers=bearer(login),
        json={"client_device_id": device, "platform": "android", **extra},
    )
    assert response.status_code == 200, response.text


async def _device_model(client, client_device_id: str) -> str | None:
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        return (
            await session.execute(
                select(UserDevice.device_model).where(
                    UserDevice.client_device_id == client_device_id
                )
            )
        ).scalar_one()


async def test_login_stores_the_model(client) -> None:
    await do_login(client, "model-login", model="Google Pixel 8")
    assert await _device_model(client, "model-login") == "Google Pixel 8"


async def test_register_stores_and_updates_the_model(client) -> None:
    login = await do_login(client, "model-register")
    assert await _device_model(client, "model-register") is None
    await register(client, login, "model-register", device_model="Google Pixel 8")
    assert await _device_model(client, "model-register") == "Google Pixel 8"
    await register(client, login, "model-register", device_model="Google Pixel 9")
    assert await _device_model(client, "model-register") == "Google Pixel 9"


async def test_register_without_the_field_keeps_the_stored_model(client) -> None:
    login = await do_login(client, "model-keep", model="Google Pixel 8")
    await register(client, login, "model-keep")
    assert await _device_model(client, "model-keep") == "Google Pixel 8"


async def test_an_overlong_model_is_rejected(client) -> None:
    login = await do_login(client, "model-long")
    response = await client.post(
        "/v3/devices/register",
        headers=bearer(login),
        json={
            "client_device_id": "model-long",
            "platform": "android",
            "device_model": "x" * 65,
        },
    )
    assert response.status_code == 422
