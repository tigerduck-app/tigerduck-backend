"""End-to-end tests for /v3/bulletin-subscriptions and /v3/bulletin-states."""

from __future__ import annotations

import pytest

from server.auth.moodle import MoodleVerifyResult, StaticMoodleVerifier
from server.bulletins.models import Bulletin
from server.db import build_session_factory

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


async def seed_bulletin(client) -> int:
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        bulletin = Bulletin(
            external_id="ext-1",
            title="期末考公告",
            source_url="https://bulletin.ntust.edu.tw/x",
        )
        session.add(bulletin)
        await session.commit()
        return bulletin.id


async def test_subscription_crud_lifecycle(client) -> None:
    login = await do_login(client)

    create = await client.post(
        "/v3/bulletin-subscriptions",
        headers=bearer(login),
        json={"name": "教務處重要公告", "orgs": ["教務處"], "tags": ["修課"], "mode": "AND"},
    )
    assert create.status_code == 200, create.text
    sub = create.json()
    assert sub["revision"] == 1
    sub_id = sub["id"]

    listing = await client.get(
        "/v3/bulletin-subscriptions", headers=bearer(login)
    )
    assert len(listing.json()["items"]) == 1

    patch = await client.patch(
        f"/v3/bulletin-subscriptions/{sub_id}",
        headers=bearer(login),
        json={"base_revision": 1, "tags": ["修課", "考試"], "enabled": False},
    )
    assert patch.status_code == 200
    assert patch.json()["revision"] == 2
    assert patch.json()["tags"] == ["修課", "考試"]
    assert patch.json()["enabled"] is False

    stale = await client.patch(
        f"/v3/bulletin-subscriptions/{sub_id}",
        headers=bearer(login),
        json={"base_revision": 1, "name": "stale"},
    )
    assert stale.status_code == 409
    assert stale.json()["server"]["revision"] == 2

    delete = await client.delete(
        f"/v3/bulletin-subscriptions/{sub_id}", headers=bearer(login)
    )
    assert delete.status_code == 204

    listing = await client.get(
        "/v3/bulletin-subscriptions", headers=bearer(login)
    )
    assert listing.json()["items"] == []


async def test_subscription_cross_user_isolation(client) -> None:
    login_a = await do_login(client)
    create = await client.post(
        "/v3/bulletin-subscriptions",
        headers=bearer(login_a),
        json={"orgs": ["教務處"]},
    )
    sub_id = create.json()["id"]

    body = dict(LOGIN_BODY, student_id="B11015999")
    body["device_info"] = dict(LOGIN_BODY["device_info"], client_device_id="dev-b")
    login_b_resp = await client.post("/v3/auth/login", json=body)
    login_b = login_b_resp.json()

    response = await client.delete(
        f"/v3/bulletin-subscriptions/{sub_id}", headers=bearer(login_b)
    )
    assert response.status_code == 404


async def test_bulletin_state_put_and_get(client) -> None:
    login = await do_login(client)
    bulletin_id = await seed_bulletin(client)

    put = await client.put(
        f"/v3/bulletin-states/{bulletin_id}",
        headers=bearer(login),
        json={
            "is_read": True,
            "read_updated_at": "2026-06-08T10:00:00Z",
            "is_starred": True,
            "starred_updated_at": "2026-06-08T10:00:00Z",
        },
    )
    assert put.status_code == 200, put.text
    state = put.json()
    assert state["is_read"] is True
    assert state["is_starred"] is True
    assert state["first_read_at"] is not None

    # Older un-star loses; newer un-read wins.
    merge = await client.put(
        f"/v3/bulletin-states/{bulletin_id}",
        headers=bearer(login),
        json={
            "is_starred": False,
            "starred_updated_at": "2026-06-08T09:00:00Z",
            "is_read": False,
            "read_updated_at": "2026-06-08T11:00:00Z",
        },
    )
    assert merge.status_code == 200
    merged = merge.json()
    assert merged["is_starred"] is True
    assert merged["is_read"] is False
    # first_read_at is a high-water mark — unread doesn't erase it.
    assert merged["first_read_at"] is not None

    get = await client.get(
        f"/v3/bulletin-states?bulletin_ids={bulletin_id}", headers=bearer(login)
    )
    assert get.status_code == 200
    assert len(get.json()["items"]) == 1


async def test_bulletin_state_unknown_bulletin_404(client) -> None:
    login = await do_login(client)
    response = await client.put(
        "/v3/bulletin-states/999999",
        headers=bearer(login),
        json={"is_read": True, "read_updated_at": "2026-06-08T10:00:00Z"},
    )
    assert response.status_code == 404


async def test_delete_with_base_revision_cas(client) -> None:
    """Greptile #3: DELETE accepts an optional base_revision — supplied
    and stale → 409 with server state, nothing deleted; supplied and
    current → 204. (Bare DELETE stays valid: independent-entity spec.)"""
    login = await do_login(client)
    create = await client.post(
        "/v3/bulletin-subscriptions",
        headers=bearer(login),
        json={"orgs": ["教務處"]},
    )
    sub_id = create.json()["id"]

    # Another device edits → revision 2.
    patch = await client.patch(
        f"/v3/bulletin-subscriptions/{sub_id}",
        headers=bearer(login),
        json={"base_revision": 1, "name": "edited elsewhere"},
    )
    assert patch.status_code == 200

    # Delete decided on the stale revision-1 read → conflict, kept alive.
    stale = await client.delete(
        f"/v3/bulletin-subscriptions/{sub_id}?base_revision=1",
        headers=bearer(login),
    )
    assert stale.status_code == 409
    assert stale.json()["server"]["revision"] == 2
    listing = await client.get(
        "/v3/bulletin-subscriptions", headers=bearer(login)
    )
    assert len(listing.json()["items"]) == 1

    # Re-read and delete with the current revision → goes through.
    ok = await client.delete(
        f"/v3/bulletin-subscriptions/{sub_id}?base_revision=2",
        headers=bearer(login),
    )
    assert ok.status_code == 204
    listing = await client.get(
        "/v3/bulletin-subscriptions", headers=bearer(login)
    )
    assert listing.json()["items"] == []
