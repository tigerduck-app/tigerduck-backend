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


CLUB_SUBSCRIPTIONS = {
    "clubs": {
        "93": {
            "subscribed": True,
            "subscribed_at": "2026-11-02T14:00:00.000Z",
            "color": "#D35400",
            "color_at": "2026-11-02T14:00:00.000Z",
        }
    },
    "order": {"ids": [93, 33], "at": "2026-11-02T14:05:00.000Z"},
}


def every_club_subscribed(count: int = 120) -> dict:
    """A student subscribed to every club clubs.ntust.edu.tw lists, about 17 KB of JSON."""
    stamp = "2026-11-02T14:00:00.000Z"
    return {
        "clubs": {
            str(club): {
                "subscribed": True,
                "subscribed_at": stamp,
                "color": "#D35400",
                "color_at": stamp,
            }
            for club in range(1000, 1000 + count)
        },
        "order": {"ids": list(range(1000, 1000 + count)), "at": stamp},
    }


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


async def test_club_subscriptions_are_stored_and_returned_unchanged(client) -> None:
    login = await do_login(client)
    path = "/v3/settings/club_subscriptions"
    create = await client.put(
        path,
        headers=bearer(login),
        json={"schema_version": 1, "document": CLUB_SUBSCRIPTIONS, "base_revision": None},
    )
    assert create.status_code == 200, create.text
    assert create.json()["revision"] == 1
    assert create.json()["document"] == CLUB_SUBSCRIPTIONS
    read = await client.get(path, headers=bearer(login))
    assert read.status_code == 200
    assert read.json()["document"] == CLUB_SUBSCRIPTIONS

    every_club = every_club_subscribed()
    replace = await client.put(
        path,
        headers=bearer(login),
        json={"schema_version": 1, "document": every_club, "base_revision": 1},
    )
    assert replace.status_code == 200, replace.text
    assert replace.json()["revision"] == 2
    assert replace.json()["document"] == every_club

    stale = await client.put(
        path,
        headers=bearer(login),
        json={"schema_version": 1, "document": CLUB_SUBSCRIPTIONS, "base_revision": 1},
    )
    assert stale.status_code == 409
    assert stale.json()["server"]["revision"] == 2
    assert stale.json()["server"]["document"] == every_club

    batch = await client.get(
        "/v3/settings?namespaces=club_subscriptions", headers=bearer(login)
    )
    assert [i["document"] for i in batch.json()["items"]] == [every_club]
    v4 = await client.get("/v4/settings/club_subscriptions", headers=bearer(login))
    assert v4.status_code == 200, v4.text
    assert v4.json()["document"] == every_club


async def test_both_snapshots_return_club_subscriptions(client) -> None:
    login = await do_login(client)
    await client.put(
        "/v3/settings/club_subscriptions",
        headers=bearer(login),
        json={"schema_version": 1, "document": CLUB_SUBSCRIPTIONS, "base_revision": None},
    )
    for path in ("/v3/sync/full", "/v4/sync/full"):
        response = await client.get(path, headers=bearer(login))
        assert response.status_code == 200, (path, response.text)
        documents = {
            d["namespace"]: d["document"] for d in response.json()["settings_documents"]
        }
        assert documents == {"club_subscriptions": CLUB_SUBSCRIPTIONS}, path
