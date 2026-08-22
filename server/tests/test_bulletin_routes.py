"""HTTP integration tests for the /v3 bulletin feed and subscription PUT.

Uses the `client` fixture from conftest which swaps in a fresh DB per test
(unlike the session-scoped `prepared_engine` used by lower-level tests),
so we get clean state for each HTTP-level scenario.

The v2 device-scoped surface is retired (410 Gone). The read-only feed now
lives at /v3/bulletins (server/routes/bulletins_feed.py, public GETs) and
snapshot-style rule replacement lives at PUT /v3/bulletin-subscriptions
(server/routes/bulletins_v3.py, JWT-scoped). Per-rule subscription CRUD is
covered separately in test_bulletins_v3_api.py.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from server.auth.moodle import MoodleVerifyResult, StaticMoodleVerifier
from server.bulletins.models import Bulletin, BulletinProcessingState
from server.bulletins.taxonomy import CanonicalOrg, ContentTag

pytestmark = pytest.mark.asyncio(loop_scope="session")


# --- Taxonomy -------------------------------------------------------------


async def test_taxonomy_lists_every_org_and_tag(client: AsyncClient) -> None:
    resp = await client.get("/v3/bulletins/taxonomy")
    assert resp.status_code == 200
    body = resp.json()
    assert {o["id"] for o in body["orgs"]} == {o.value for o in CanonicalOrg}
    assert {t["id"] for t in body["tags"]} == {t.value for t in ContentTag}
    # free_meal must be present — regression guard
    assert "free_meal" in {t["id"] for t in body["tags"]}
    assert "free_meal" in body["default_tags"]
    assert all(o["label"] for o in body["orgs"])
    assert all(t["label"] for t in body["tags"])


# --- Bulletin list / detail ----------------------------------------------


async def _seed_bulletins(client: AsyncClient, count: int = 3) -> list[int]:
    factory: async_sessionmaker = client.app.state.session_factory  # type: ignore[attr-defined]
    ids: list[int] = []
    async with factory() as session:
        for i in range(count):
            await session.execute(
                insert(Bulletin).values(
                    source="ntust_general",
                    external_id=f"route-{1000 + i}",
                    source_url=f"https://x/{1000 + i}",
                    raw_publisher="test",
                    title=f"title {i}",
                    body_md="body",
                    canonical_org=CanonicalOrg.library.value,
                    content_tags=[ContentTag.event.value],
                    summary=f"sum {i}",
                    body_clean="clean",
                    importance="normal",
                    processing_state=BulletinProcessingState.processed.value,
                )
            )
        await session.commit()

        from sqlalchemy import select as s

        rows = (
            await session.execute(
                s(Bulletin.id).where(Bulletin.external_id.like("route-%")).order_by(Bulletin.id)
            )
        ).scalars().all()
        ids.extend(rows)
    return ids


async def test_list_bulletins_newest_first_paginates(client: AsyncClient) -> None:
    ids = await _seed_bulletins(client, count=5)
    resp = await client.get("/v3/bulletins?limit=2")
    assert resp.status_code == 200
    body = resp.json()
    returned_ids = [item["id"] for item in body["items"]]
    assert returned_ids == sorted(returned_ids, reverse=True)
    assert len(returned_ids) == 2
    assert body["next_cursor"] == returned_ids[-1]

    # Fetch the next page via cursor
    resp2 = await client.get(f"/v3/bulletins?limit=2&cursor={body['next_cursor']}")
    next_ids = [item["id"] for item in resp2.json()["items"]]
    assert max(next_ids) < min(returned_ids)


async def test_list_bulletins_hides_deleted_by_default(client: AsyncClient) -> None:
    ids = await _seed_bulletins(client, count=2)
    factory: async_sessionmaker = client.app.state.session_factory  # type: ignore[attr-defined]
    async with factory() as session:
        from sqlalchemy import update

        await session.execute(
            update(Bulletin).where(Bulletin.id == ids[0]).values(is_deleted=True)
        )
        await session.commit()

    resp = await client.get("/v3/bulletins")
    returned_ids = {item["id"] for item in resp.json()["items"]}
    assert ids[0] not in returned_ids
    assert ids[1] in returned_ids

    resp_with_deleted = await client.get("/v3/bulletins?include_deleted=true")
    with_ids = {item["id"] for item in resp_with_deleted.json()["items"]}
    assert ids[0] in with_ids


async def test_get_bulletin_returns_detail(client: AsyncClient) -> None:
    ids = await _seed_bulletins(client, count=1)
    resp = await client.get(f"/v3/bulletins/{ids[0]}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == ids[0]
    assert body["body_clean"] == "clean"
    assert body["body_md"] == "body"
    assert body["canonical_org"] == CanonicalOrg.library.value


async def test_get_bulletin_404s_when_missing(client: AsyncClient) -> None:
    resp = await client.get("/v3/bulletins/999999999")
    assert resp.status_code == 404


# --- Subscription snapshot PUT ---------------------------------------------


_LOGIN_BODY = {
    "student_id": "B11015000",
    "password": "pw",
    "moodle_token": "tok",
    "device_info": {"client_device_id": "dev-routes", "platform": "ios"},
}


async def _login(client: AsyncClient) -> dict:
    client.app.state.moodle_verifier = StaticMoodleVerifier(  # type: ignore[attr-defined]
        MoodleVerifyResult(ok=True, username="b11015000")
    )
    resp = await client.post("/v3/auth/login", json=_LOGIN_BODY)
    assert resp.status_code == 200
    return resp.json()


def _bearer(login: dict) -> dict:
    return {"Authorization": f"Bearer {login['access_token']}"}


async def test_list_subscriptions_empty_then_after_put(
    client: AsyncClient,
) -> None:
    login = await _login(client)

    empty = await client.get("/v3/bulletin-subscriptions", headers=_bearer(login))
    assert empty.status_code == 200
    assert empty.json()["items"] == []

    put = await client.put(
        "/v3/bulletin-subscriptions",
        headers=_bearer(login),
        json={
            "rules": [
                {
                    "name": "學務處獎學金",
                    "orgs": [CanonicalOrg.student_affairs.value],
                    "tags": [ContentTag.scholarship.value],
                    "mode": "AND",
                    "enabled": True,
                },
                {
                    "name": "任何免費便當",
                    "orgs": [],
                    "tags": [ContentTag.free_meal.value],
                    "mode": "AND",
                    "enabled": True,
                },
            ]
        },
    )
    assert put.status_code == 200
    saved = put.json()["items"]
    assert len(saved) == 2
    assert {r["name"] for r in saved} == {"學務處獎學金", "任何免費便當"}
    assert all(r["id"] for r in saved)

    again = await client.get("/v3/bulletin-subscriptions", headers=_bearer(login))
    assert [r["name"] for r in again.json()["items"]] == [
        "學務處獎學金",
        "任何免費便當",
    ]


async def test_put_subscriptions_is_snapshot_replacement(
    client: AsyncClient,
) -> None:
    """Second PUT with a shorter list removes the extras."""
    login = await _login(client)

    await client.put(
        "/v3/bulletin-subscriptions",
        headers=_bearer(login),
        json={
            "rules": [
                {"orgs": [CanonicalOrg.library.value], "tags": [], "mode": "AND"},
                {"orgs": [CanonicalOrg.pe.value], "tags": [], "mode": "AND"},
            ]
        },
    )
    await client.put(
        "/v3/bulletin-subscriptions",
        headers=_bearer(login),
        json={
            "rules": [
                {"orgs": [CanonicalOrg.library.value], "tags": [], "mode": "AND"}
            ]
        },
    )
    got = await client.get("/v3/bulletin-subscriptions", headers=_bearer(login))
    rules = got.json()["items"]
    assert len(rules) == 1
    assert rules[0]["orgs"] == [CanonicalOrg.library.value]


async def test_subscriptions_put_requires_auth(client: AsyncClient) -> None:
    """PUT without a bearer token is rejected so a saved ruleset can't end
    up orphaned with no authenticated user to attach it to (the v3 analogue
    of the old "PUT 404s for unregistered devices" guard)."""
    put = await client.put(
        "/v3/bulletin-subscriptions",
        json={"rules": []},
    )
    assert put.status_code == 401
