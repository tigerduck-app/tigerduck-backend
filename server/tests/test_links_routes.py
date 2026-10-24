"""`GET /links/{name}`, which the apps follow without signing in, and the
operator routes on the admin listener that edit the links."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from prometheus_client import REGISTRY
from sqlalchemy import select

from server import metrics
from server.admin_audit import AdminAuditLog
from server.links import checker
from server.links.models import ALERT_FAILING, Link
from server.links.rules import Page

pytestmark = pytest.mark.asyncio(loop_scope="session")

CALENDAR = "https://www.academic.ntust.edu.tw/p/404-1048-78935.php"
ACTOR = {"X-Admin-Actor": "ops@example.com"}


async def _add(client: AsyncClient, name: str, target: str = CALENDAR, **fields) -> None:
    async with client.app.state.session_factory() as session:
        session.add(Link(name=name, target_url=target, updated_by="test", **fields))
        await session.commit()


async def _row(client: AsyncClient, name: str) -> Link | None:
    async with client.app.state.session_factory() as session:
        return await session.get(Link, name)


async def _audit(client: AsyncClient) -> list[tuple[str, str, dict]]:
    async with client.app.state.session_factory() as session:
        rows = (
            await session.execute(select(AdminAuditLog).order_by(AdminAuditLog.id))
        ).scalars()
        return [(r.actor, r.action, r.detail) for r in rows]


# --- the public redirect ----------------------------------------------------


async def test_a_link_answers_307_to_its_target_and_is_not_cached(client) -> None:
    await _add(client, "ntust-calendar")

    resp = await client.get("/links/ntust-calendar")

    assert resp.status_code == 307
    assert resp.headers["location"] == CALENDAR
    assert resp.headers["cache-control"] == "no-store"


async def test_the_redirect_needs_no_auth_even_with_the_shared_secret_set(client) -> None:
    await _add(client, "ntust-calendar")
    client.app.state.settings = client.app.state.settings.model_copy(
        update={"api_shared_secret": "s3cret"}
    )

    resp = await client.get("/links/ntust-calendar")

    assert resp.status_code == 307
    assert "authorization" not in resp.request.headers


@pytest.mark.parametrize(
    ("path", "row"),
    [
        ("/links/nothing-here", None),
        ("/links/ntust-calendar", {"enabled": False}),
        ("/links/ntust-calendar", {"target": "https://evil.example/"}),
        ("/links/ntust-calendar", {"target": "http://www.ntust.edu.tw/"}),
        ("/links/NTUST-CALENDAR", {}),
        ("/links/ntust_calendar", {}),
    ],
)
async def test_unknown_disabled_or_disallowed_links_answer_404(client, path, row) -> None:
    if row is not None:
        await _add(client, "ntust-calendar", **row)

    resp = await client.get(path)

    assert resp.status_code == 404
    assert resp.json() == {"detail": "link not found"}
    assert "location" not in resp.headers


async def test_a_target_outside_the_allowed_hosts_is_refused_when_served(client) -> None:
    await _add(client, "library", target="https://library.example/")
    assert (await client.get("/links/library")).status_code == 404

    client.app.state.settings = client.app.state.settings.model_copy(
        update={"links_allowed_hosts": ["library.example"]}
    )
    assert (await client.get("/links/library")).status_code == 307


def _requests(route: str, status: str) -> float:
    value = REGISTRY.get_sample_value(
        "tigerduck_http_requests_total",
        {"method": "GET", "route": route, "status": status},
    )
    return value or 0.0


async def test_requests_are_counted_by_the_route_template_not_the_name(client) -> None:
    await _add(client, "ntust-calendar")
    before = _requests("/links/{name}", "307"), _requests("/links/{name}", "404")

    await client.get("/links/ntust-calendar")
    await client.get("/links/some-other-name")

    assert _requests("/links/{name}", "307") == before[0] + 1
    assert _requests("/links/{name}", "404") == before[1] + 1
    assert _requests("/links/some-other-name", "404") == 0


async def test_the_api_port_serves_no_admin_routes(client) -> None:
    assert (await client.get("/admin/links")).status_code == 404


# --- the admin routes -------------------------------------------------------


async def test_create_stores_the_link_and_audits_it(admin_client, client) -> None:
    resp = await admin_client.post(
        "/admin/links",
        headers=ACTOR,
        json={
            "name": "ntust-calendar",
            "target_url": CALENDAR,
            "expectations": [
                {"type": "status", "equals": 200},
                {"type": "final_host", "in": ["www.academic.ntust.edu.tw"]},
            ],
        },
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["check_interval_minutes"] == 60
    assert body["expectations"][1] == {
        "type": "final_host",
        "in": ["www.academic.ntust.edu.tw"],
    }
    assert body["updated_by"] == "ops@example.com"
    assert (await client.get("/links/ntust-calendar")).status_code == 307
    assert await _audit(client) == [
        (
            "ops@example.com",
            "link.create",
            {"name": "ntust-calendar", "target_url": CALENDAR},
        )
    ]


@pytest.mark.parametrize(
    "target",
    [
        "http://www.ntust.edu.tw/",
        "https://user:pw@www.ntust.edu.tw/",
        "https://www.ntust.edu.tw/\tx",
        "https://www.ntust.edu.tw/\x7f",
        "https://evil.example/",
    ],
)
async def test_create_refuses_an_invalid_target(admin_client, client, target) -> None:
    resp = await admin_client.post(
        "/admin/links", headers=ACTOR, json={"name": "bad", "target_url": target}
    )

    assert resp.status_code == 422
    assert resp.json()["detail"].startswith("invalid target: ")
    assert await _row(client, "bad") is None
    assert await _audit(client) == []


@pytest.mark.parametrize(
    "body",
    [
        {"name": "Bad_Name", "target_url": CALENDAR},
        {"name": "ok", "target_url": CALENDAR, "expectations": [{"type": "regex"}]},
        {"name": "ok", "target_url": CALENDAR, "check_interval_minutes": 1},
        {"name": "ok", "target_url": CALENDAR, "extra": True},
    ],
)
async def test_create_refuses_an_invalid_body(admin_client, client, body) -> None:
    resp = await admin_client.post("/admin/links", headers=ACTOR, json=body)

    assert resp.status_code == 422
    assert await _audit(client) == []


async def test_create_refuses_a_name_in_use_and_a_call_without_an_actor(
    admin_client, client
) -> None:
    await _add(client, "ntust-calendar")
    body = {"name": "ntust-calendar", "target_url": "https://www.ntust.edu.tw/"}

    assert (await admin_client.post("/admin/links", headers=ACTOR, json=body)).status_code == 409
    assert (await admin_client.post("/admin/links", json=body)).status_code == 422
    assert (await _row(client, "ntust-calendar")).target_url == CALENDAR
    assert await _audit(client) == []


async def test_the_admin_routes_check_the_shared_secret(admin_client, client) -> None:
    client.app.state.settings = client.app.state.settings.model_copy(
        update={"api_shared_secret": "s3cret"}
    )

    assert (await admin_client.get("/admin/links")).status_code == 401
    resp = await admin_client.get("/admin/links", headers={"X-Push-Token": "s3cret"})
    assert resp.status_code == 200


async def test_edit_and_disable_are_audited_by_what_changed(admin_client, client) -> None:
    await _add(client, "ntust-calendar")

    resp = await admin_client.patch(
        "/admin/links/ntust-calendar",
        headers=ACTOR,
        json={"target_url": "https://www.ntust.edu.tw/calendar", "check_interval_minutes": 30},
    )
    assert resp.status_code == 200, resp.text
    assert (await client.get("/links/ntust-calendar")).headers["location"] == (
        "https://www.ntust.edu.tw/calendar"
    )

    resp = await admin_client.patch(
        "/admin/links/ntust-calendar", headers=ACTOR, json={"enabled": False}
    )
    assert resp.json()["enabled"] is False
    assert (await client.get("/links/ntust-calendar")).status_code == 404

    # Nothing changes, so nothing is recorded.
    await admin_client.patch(
        "/admin/links/ntust-calendar", headers=ACTOR, json={"enabled": False}
    )

    assert await _audit(client) == [
        (
            "ops@example.com",
            "link.update",
            {
                "name": "ntust-calendar",
                "target_url": "https://www.ntust.edu.tw/calendar",
                "check_interval_minutes": 30,
            },
        ),
        ("ops@example.com", "link.disable", {"name": "ntust-calendar", "enabled": False}),
    ]


@pytest.mark.parametrize(
    ("change", "rechecked"),
    [
        ({"target_url": "https://www.ntust.edu.tw/calendar"}, True),
        ({"expectations": [{"type": "contains", "text": "行事曆"}]}, True),
        ({"check_interval_minutes": 30}, False),
        ({"enabled": False}, False),
    ],
)
async def test_a_new_target_or_new_rules_are_checked_at_the_next_tick(
    admin_client, client, change, rechecked
) -> None:
    from datetime import UTC, datetime

    checked = datetime.now(UTC)
    await _add(client, "ntust-calendar", last_check_at=checked)

    resp = await admin_client.patch("/admin/links/ntust-calendar", headers=ACTOR, json=change)

    assert resp.status_code == 200, resp.text
    row = await _row(client, "ntust-calendar")
    assert row.last_check_at == (None if rechecked else checked)


async def test_edit_refuses_an_invalid_target_and_an_unknown_link(admin_client, client) -> None:
    await _add(client, "ntust-calendar")

    resp = await admin_client.patch(
        "/admin/links/ntust-calendar",
        headers=ACTOR,
        json={"target_url": "https://evil.example/"},
    )
    assert resp.status_code == 422
    assert (await _row(client, "ntust-calendar")).target_url == CALENDAR

    resp = await admin_client.patch("/admin/links/nope", headers=ACTOR, json={"enabled": True})
    assert resp.status_code == 404
    assert await _audit(client) == []


async def test_test_now_checks_records_and_audits(admin_client, client, monkeypatch) -> None:
    await _add(
        client, "ntust-calendar", expectations=[{"type": "status", "equals": 200}]
    )
    monkeypatch.setattr(
        checker, "fetch", lambda url, hosts: Page(status=503, final_url=url, body="")
    )

    try:
        resp = await admin_client.post("/admin/links/ntust-calendar/test", headers=ACTOR)

        assert resp.status_code == 200
        assert resp.json() == {"ok": False, "detail": "status: expected 200, found 503"}
        row = await _row(client, "ntust-calendar")
        assert (row.last_check_ok, row.alert_state) == (False, ALERT_FAILING)
        assert await _audit(client) == [
            ("ops@example.com", "link.test", {"name": "ntust-calendar", "ok": False})
        ]
        listed = (await admin_client.get("/admin/links")).json()["links"]
        assert [(link["name"], link["last_check_ok"]) for link in listed] == [
            ("ntust-calendar", False)
        ]
        resp = await admin_client.post("/admin/links/nope/test", headers=ACTOR)
        assert resp.status_code == 404
    finally:
        metrics.forget_link("ntust-calendar")
