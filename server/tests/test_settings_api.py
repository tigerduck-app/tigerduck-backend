"""End-to-end tests for /v3/settings optimistic-concurrency sync."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from server.auth.moodle import MoodleVerifyResult, StaticMoodleVerifier
from server.db import build_session_factory
from server.sync.models import UserChangeLog

pytestmark = pytest.mark.asyncio(loop_scope="session")

LOGIN_BODY = {
    "student_id": "B11015000",
    "password": "pw",
    "moodle_token": "tok",
    "device_info": {"client_device_id": "iphone-abc", "platform": "ios"},
}


async def do_login(client) -> dict:
    client.app.state.moodle_verifier = StaticMoodleVerifier(
        MoodleVerifyResult(ok=True, username="b11015000")
    )
    response = await client.post("/v3/auth/login", json=LOGIN_BODY)
    assert response.status_code == 200
    return response.json()


def bearer(login: dict) -> dict:
    return {"Authorization": f"Bearer {login['access_token']}"}


async def test_create_and_read_settings_document(client) -> None:
    login = await do_login(client)
    create = await client.put(
        "/v3/settings/home_layout",
        headers=bearer(login),
        json={
            "schema_version": 1,
            "document": {"sections": ["schedule", "assignments"]},
            "base_revision": None,
        },
    )
    assert create.status_code == 200, create.text
    body = create.json()
    assert body["namespace"] == "home_layout"
    assert body["revision"] == 1
    assert body["document"]["sections"] == ["schedule", "assignments"]

    read = await client.get("/v3/settings/home_layout", headers=bearer(login))
    assert read.status_code == 200
    assert read.json()["revision"] == 1


async def test_update_with_correct_base_revision(client) -> None:
    login = await do_login(client)
    await client.put(
        "/v3/settings/appearance",
        headers=bearer(login),
        json={"schema_version": 1, "document": {"theme": "dark"}, "base_revision": None},
    )
    update = await client.put(
        "/v3/settings/appearance",
        headers=bearer(login),
        json={"schema_version": 1, "document": {"theme": "light"}, "base_revision": 1},
    )
    assert update.status_code == 200
    assert update.json()["revision"] == 2
    assert update.json()["document"] == {"theme": "light"}


async def test_stale_base_revision_409_returns_server_state(client) -> None:
    login = await do_login(client)
    await client.put(
        "/v3/settings/appearance",
        headers=bearer(login),
        json={"schema_version": 1, "document": {"theme": "dark"}, "base_revision": None},
    )
    await client.put(
        "/v3/settings/appearance",
        headers=bearer(login),
        json={"schema_version": 1, "document": {"theme": "light"}, "base_revision": 1},
    )
    stale = await client.put(
        "/v3/settings/appearance",
        headers=bearer(login),
        json={"schema_version": 1, "document": {"theme": "blue"}, "base_revision": 1},
    )
    assert stale.status_code == 409
    body = stale.json()
    assert body["error"] == "settings_conflict"
    assert body["server"]["revision"] == 2
    assert body["server"]["document"] == {"theme": "light"}


async def test_create_when_namespace_exists_409(client) -> None:
    login = await do_login(client)
    await client.put(
        "/v3/settings/appearance",
        headers=bearer(login),
        json={"schema_version": 1, "document": {"theme": "dark"}, "base_revision": None},
    )
    duplicate = await client.put(
        "/v3/settings/appearance",
        headers=bearer(login),
        json={"schema_version": 1, "document": {"theme": "red"}, "base_revision": None},
    )
    assert duplicate.status_code == 409


async def test_unknown_namespace_422(client) -> None:
    login = await do_login(client)
    response = await client.put(
        "/v3/settings/not_a_namespace",
        headers=bearer(login),
        json={"schema_version": 1, "document": {}, "base_revision": None},
    )
    assert response.status_code == 422


async def test_batch_read(client) -> None:
    login = await do_login(client)
    for namespace, doc in (
        ("home_layout", {"sections": []}),
        ("appearance", {"theme": "dark"}),
    ):
        await client.put(
            f"/v3/settings/{namespace}",
            headers=bearer(login),
            json={"schema_version": 1, "document": doc, "base_revision": None},
        )
    response = await client.get(
        "/v3/settings?namespaces=home_layout,appearance,notification",
        headers=bearer(login),
    )
    assert response.status_code == 200
    items = response.json()["items"]
    assert {i["namespace"] for i in items} == {"home_layout", "appearance"}


async def test_write_appends_pointer_only_changelog(client) -> None:
    login = await do_login(client)
    await client.put(
        "/v3/settings/appearance",
        headers=bearer(login),
        json={"schema_version": 1, "document": {"theme": "dark"}, "base_revision": None},
    )
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        entry = (
            await session.execute(
                select(UserChangeLog).where(
                    UserChangeLog.entity_type == "settings_document"
                )
            )
        ).scalar_one()
        assert entry.entity_id == "appearance"
        # Pointer only — never the document body.
        assert entry.payload == {"revision": 1}
