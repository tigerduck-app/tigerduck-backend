"""`/v4`'s bulletin subscriptions: each rule kept by the `client_id` its
client made (docs/api-v4.md, "Subscriptions")."""

from __future__ import annotations

import uuid

import pytest

from server.tests.test_api_v4 import bearer, login

pytestmark = pytest.mark.asyncio(loop_scope="session")

PATH = "/v4/bulletin-subscriptions"
A, B, C = (str(uuid.uuid4()) for _ in range(3))


def rule(client_id: str, name: str, **fields) -> dict:
    return {"client_id": client_id, "name": name, "orgs": ["ntust"], "tags": [], **fields}


async def put(client, session, *rules: dict):
    return await client.put(PATH, headers=bearer(session), json={"rules": list(rules)})


async def test_a_put_keeps_each_rule_by_its_client_id(client) -> None:
    session = await login(client)
    first = (await put(client, session, rule(A, "a"), rule(B, "b"))).json()["items"]
    ids = {item["client_id"]: item["id"] for item in first}

    second = await put(client, session, rule(C, "c"), rule(A, "a", enabled=False), rule(B, "b"))

    assert second.status_code == 200, second.text
    items = second.json()["items"]
    assert [(i["client_id"], i["name"], i["enabled"], i["revision"]) for i in items] == [
        (C, "c", True, 1),
        (A, "a", False, 2),
        (B, "b", True, 1),
    ]
    assert (items[1]["id"], items[2]["id"]) == (ids[A], ids[B])
    listed = (await client.get(PATH, headers=bearer(session))).json()["items"]
    assert sorted(listed, key=lambda i: i["id"]) == sorted(items, key=lambda i: i["id"])
    assert {"id", "client_id", "name", "orgs", "tags", "mode", "enabled", "revision"} <= listed[0].keys()


async def test_a_rule_left_out_is_deleted_and_its_client_id_freed(client) -> None:
    session = await login(client)
    await put(client, session, rule(A, "a"), rule(B, "b"))

    await put(client, session, rule(B, "b"))
    back = (await put(client, session, rule(A, "a again"), rule(B, "b"))).json()["items"]

    assert [(i["client_id"], i["name"]) for i in back] == [(A, "a again"), (B, "b")]
    listed = (await client.get(PATH, headers=bearer(session))).json()["items"]
    assert sorted(i["client_id"] for i in listed) == sorted([A, B])


async def test_a_duplicate_or_missing_client_id_is_invalid_request(client) -> None:
    session = await login(client)
    duplicate = await put(client, session, rule(A, "a"), rule(A, "b"))
    missing = await client.put(
        PATH, headers=bearer(session), json={"rules": [{"name": "a"}]}
    )

    for response in (duplicate, missing):
        assert response.status_code == 422
        assert response.json()["error"] == "invalid_request"
    assert (await client.get(PATH, headers=bearer(session))).json()["items"] == []


async def test_rules_belong_to_their_device(client) -> None:
    phone = await login(client)
    await put(client, phone, rule(A, "phone"))
    pad = await login(client, device="ipad")
    assert pad["user"]["id"] == phone["user"]["id"]
    await put(client, pad, rule(A, "pad"))

    phone_rules = (await client.get(PATH, headers=bearer(phone))).json()["items"]
    assert [(i["client_id"], i["name"]) for i in phone_rules] == [(A, "phone")]


async def test_v3_writes_get_a_client_id_and_v3_reads_show_none(client) -> None:
    session = await login(client, prefix="/v3")
    created = await client.put(
        "/v3/bulletin-subscriptions",
        headers=bearer(session),
        json={"rules": [{"name": "old", "orgs": ["ntust"]}]},
    )
    (v3_rule,) = created.json()["items"]

    (v4_rule,) = (await client.get(PATH, headers=bearer(session))).json()["items"]
    assert v4_rule == {**v3_rule, "client_id": v4_rule["client_id"]}
    uuid.UUID(v4_rule["client_id"])

    kept = (await put(client, session, rule(v4_rule["client_id"], "old"))).json()["items"]
    assert (kept[0]["id"], kept[0]["revision"]) == (v3_rule["id"], 1)
    (v3_read,) = (await client.get("/v3/bulletin-subscriptions", headers=bearer(session))).json()["items"]
    assert "client_id" not in v3_read
