"""End-to-end tests for GET /v3/sync and GET /v3/sync/full."""

from __future__ import annotations

import uuid

import pytest

from server.auth.moodle import MoodleVerifyResult, StaticMoodleVerifier
from server.db import build_session_factory
from server.sync.changelog import append_change
from server.sync.models import UserAssignment, UserCourse, UserSettingsDocument
from server.sync.models import UserSyncState

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


async def seed_changes(client, user_id: str, n: int) -> list[int]:
    factory = build_session_factory(client.app.state.engine)
    revisions = []
    async with factory() as session:
        for i in range(n):
            revisions.append(
                await append_change(
                    session,
                    user_id=uuid.UUID(user_id),
                    entity_type="assignment",
                    entity_id=str(i),
                    operation="upsert",
                    payload={"fields": ["title"]},
                )
            )
        await session.commit()
    return revisions


async def test_sync_requires_auth(client) -> None:
    assert (await client.get("/v3/sync?since_revision=0")).status_code == 401


async def test_incremental_sync_envelope(client) -> None:
    login = await do_login(client)
    revisions = await seed_changes(client, login["user"]["id"], 3)

    response = await client.get(
        "/v3/sync?since_revision=0", headers=bearer(login)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["current_revision"] == revisions[-1]
    assert body["returned_until_revision"] == revisions[-1]
    assert body["has_more"] is False
    assert [c["revision"] for c in body["changes"]] == revisions
    assert body["changes"][0]["entity_type"] == "assignment"
    assert body["changes"][0]["operation"] == "upsert"
    assert body["changes"][0]["payload"] == {"fields": ["title"]}


async def test_incremental_sync_pagination(client) -> None:
    login = await do_login(client)
    revisions = await seed_changes(client, login["user"]["id"], 5)

    page1 = (
        await client.get(
            "/v3/sync?since_revision=0&limit=2", headers=bearer(login)
        )
    ).json()
    assert page1["has_more"] is True
    assert len(page1["changes"]) == 2

    page2 = (
        await client.get(
            f"/v3/sync?since_revision={page1['returned_until_revision']}&limit=10",
            headers=bearer(login),
        )
    ).json()
    assert page2["has_more"] is False
    collected = [c["revision"] for c in page1["changes"] + page2["changes"]]
    assert collected == revisions


async def test_expired_revision_410(client) -> None:
    login = await do_login(client)
    await seed_changes(client, login["user"]["id"], 2)

    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        state = await session.get(
            UserSyncState, uuid.UUID(login["user"]["id"])
        )
        state.compacted_revision = state.current_revision
        await session.commit()
        compacted = state.compacted_revision

    response = await client.get(
        "/v3/sync?since_revision=0", headers=bearer(login)
    )
    assert response.status_code == 410
    body = response.json()
    assert body["error"] == "sync_revision_expired"
    assert body["min_available_revision"] == compacted
    assert body["full_sync_required"] is True


async def test_full_sync_snapshot(client) -> None:
    login = await do_login(client)
    user_id = uuid.UUID(login["user"]["id"])

    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        session.add(
            UserCourse(
                user_id=user_id,
                semester="1141",
                course_key="CS3001301",
                course_no="CS3001301",
                course_name="作業系統",
            )
        )
        session.add(
            UserAssignment(
                user_id=user_id,
                moodle_course_id=101,
                moodle_assignment_id=9001,
                title="HW3",
            )
        )
        session.add(
            UserSettingsDocument(
                user_id=user_id,
                namespace="appearance",
                document={"theme": "dark"},
            )
        )
        revision = await append_change(
            session,
            user_id=user_id,
            entity_type="course",
            entity_id="1",
            operation="upsert",
        )
        await session.commit()

    response = await client.get("/v3/sync/full", headers=bearer(login))
    assert response.status_code == 200
    body = response.json()
    assert body["current_revision"] == revision
    assert len(body["courses"]) == 1
    assert body["courses"][0]["course_key"] == "CS3001301"
    assert len(body["assignments"]) == 1
    assert body["assignments"][0]["title"] == "HW3"
    assert len(body["settings_documents"]) == 1
    assert body["settings_documents"][0]["namespace"] == "appearance"
    assert body["settings_documents"][0]["document"] == {"theme": "dark"}
    # Empty sections are present (client treats full sync as authoritative).
    assert body["course_overrides"] == []
    assert body["bulletin_subscriptions"] == []
