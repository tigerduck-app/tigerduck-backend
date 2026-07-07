"""GET/PATCH /v3/admin/sync-policies — shared-secret protected."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_list_policies_returns_seeded_defaults(client):
    response = await client.get("/v3/admin/sync-policies")
    assert response.status_code == 200
    body = {p["job_type"]: p for p in response.json()["policies"]}
    assert body["moodle_assignments"]["enabled"] is True
    assert body["moodle_assignments"]["default_interval_seconds"] == 28800
    assert body["ntust_courses"]["enabled"] is True
    assert body["calendar"]["default_interval_seconds"] == 604800


async def test_patch_updates_interval_and_enabled(client):
    response = await client.patch(
        "/v3/admin/sync-policies/ntust_courses",
        json={"enabled": True, "default_interval_seconds": 3600},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["default_interval_seconds"] == 3600


async def test_patch_sets_and_clears_active_window(client):
    window_start = datetime(2026, 6, 1, tzinfo=UTC).isoformat()
    response = await client.patch(
        "/v3/admin/sync-policies/ntust_courses",
        json={"active_from": window_start},
    )
    assert response.status_code == 200
    assert response.json()["active_from"] is not None

    response = await client.patch(
        "/v3/admin/sync-policies/ntust_courses",
        json={"active_from": None},
    )
    assert response.status_code == 200
    assert response.json()["active_from"] is None


async def test_patch_unknown_job_type_404(client):
    response = await client.patch(
        "/v3/admin/sync-policies/bogus", json={"enabled": True}
    )
    assert response.status_code == 404


async def test_patch_rejects_nonpositive_interval(client):
    response = await client.patch(
        "/v3/admin/sync-policies/calendar",
        json={"default_interval_seconds": 0},
    )
    assert response.status_code == 422


async def test_admin_requires_shared_secret_when_configured(client):
    client.app.state.settings.api_shared_secret = "sekrit"
    try:
        denied = await client.patch(
            "/v3/admin/sync-policies/calendar", json={"enabled": False}
        )
        assert denied.status_code == 401
        allowed = await client.patch(
            "/v3/admin/sync-policies/calendar",
            json={"enabled": False},
            headers={"X-Push-Token": "sekrit"},
        )
        assert allowed.status_code == 200
    finally:
        client.app.state.settings.api_shared_secret = ""
