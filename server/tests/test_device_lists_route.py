"""HTTP integration for /v2/device-lists — name validation."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_create_rejects_whitespace_only_name(client: AsyncClient):
    # A whitespace-only name passes min_length=1 but trims to "" — reject
    # it rather than storing an unusable empty-name list.
    resp = await client.post("/v2/device-lists", json={"name": "   "})
    assert resp.status_code == 422, resp.text


async def test_create_trims_name(client: AsyncClient):
    resp = await client.post("/v2/device-lists", json={"name": "  beta  "})
    assert resp.status_code == 201, resp.text
    assert resp.json()["name"] == "beta"


async def test_patch_rejects_whitespace_only_name(client: AsyncClient):
    created = await client.post("/v2/device-lists", json={"name": "spring"})
    list_id = created.json()["id"]
    resp = await client.patch(
        f"/v2/device-lists/{list_id}", json={"name": "   "}
    )
    assert resp.status_code == 422, resp.text
