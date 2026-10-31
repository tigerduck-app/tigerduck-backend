"""`/v4` on the wire (docs/api-v4.md): the routes it mounts, protocol 2
implied, the error envelope and the clock on every answer, with `/v3`'s
answers to the same requests unchanged."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.routing import iter_route_contexts
from httpx import ASGITransport, AsyncClient

from server.api_v4 import CODES, envelope
from server.auth.moodle import MoodleVerifyResult, StaticMoodleVerifier
from server.sync.merge import CLOCK_HEADER, to_clock
from server.tests.test_holiday_overrides import seed_holiday

pytestmark = pytest.mark.asyncio(loop_scope="session")

KEY = "client:1141:CS3001301"

V4_ROUTES = {
    ("POST", "/v4/auth/login"),
    ("POST", "/v4/auth/refresh"),
    ("PATCH", "/v4/auth/credentials"),
    ("POST", "/v4/devices/register"),
    ("POST", "/v4/devices/anonymous"),
    ("DELETE", "/v4/devices/{device_id}"),
    ("PATCH", "/v4/devices/{device_id}/preferences"),
    ("GET", "/v4/sync/revision"),
    ("GET", "/v4/sync"),
    ("GET", "/v4/sync/full"),
    ("POST", "/v4/sync/courses/upload"),
    ("DELETE", "/v4/sync/courses"),
    ("DELETE", "/v4/sync/courses/{course_key:path}"),
    ("POST", "/v4/sync/assignments/upload"),
    ("PATCH", "/v4/sync/assignments/{moodle_assignment_id}/override"),
    ("PATCH", "/v4/sync/courses/{course_key}/override"),
    ("PUT", "/v4/sync/holiday-overrides/{holiday_id}"),
    ("GET", "/v4/settings/{namespace}"),
    ("PUT", "/v4/settings/{namespace}"),
    ("POST", "/v4/schedule/sync"),
    ("POST", "/v4/live-activities/register"),
    ("GET", "/v4/bulletins/taxonomy"),
    ("GET", "/v4/bulletins"),
    ("GET", "/v4/bulletins/{bulletin_id}"),
    ("GET", "/v4/bulletin-subscriptions"),
    ("PUT", "/v4/bulletin-subscriptions"),
    ("GET", "/v4/calendar/semesters"),
    ("GET", "/v4/calendar/course-changes"),
    ("GET", "/v4/course-catalog/{semester}"),
}


async def login(client, prefix: str = "/v4", student_id: str = "B11015000") -> dict:
    client.app.state.moodle_verifier = StaticMoodleVerifier(
        MoodleVerifyResult(ok=True, username=student_id.lower())
    )
    response = await client.post(
        f"{prefix}/auth/login",
        json={
            "student_id": student_id,
            "moodle_token": "tok",
            "device_info": {"client_device_id": f"iphone-{student_id}", "platform": "ios"},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def bearer(login: dict) -> dict:
    return {"Authorization": f"Bearer {login['access_token']}"}


async def test_v4_mounts_the_29_routes_of_the_contract(client) -> None:
    mounted = {
        (method, context.path)
        for context in iter_route_contexts(client.app.routes)
        if context.path and context.path.startswith("/v4/")
        for method in context.methods or ()
    }
    assert mounted == V4_ROUTES


async def test_version_names_both_base_paths(client) -> None:
    body = (await client.get("/version")).json()
    assert (body["api_base_path"], body["api_v4_base_path"]) == ("/v3", "/v4")


async def test_every_v4_answer_carries_the_server_clock(client) -> None:
    session = await login(client)
    before = to_clock(datetime.now(UTC))
    answers = [
        await client.get("/v4/sync/revision", headers=bearer(session)),
        await client.get("/v4/no-such-route"),
        await client.get("/v4/sync/revision"),
        await client.patch(
            "/v4/sync/assignments/9001/override",
            headers=bearer(session),
            json={"local_status": "done"},
        ),
    ]
    after = to_clock(datetime.now(UTC))
    assert [a.status_code for a in answers] == [200, 404, 401, 422]
    for answer in answers:
        assert before <= int(answer.headers[CLOCK_HEADER]) <= after


# --- The error envelope ---------------------------------------------------------


async def test_an_unknown_route_and_method_answer_in_the_envelope(client) -> None:
    missing = await client.get("/v4/no-such-route")
    wrong = await client.get("/v4/sync/courses/upload")

    assert missing.status_code == 404
    assert missing.json() == {"error": "not_found", "message": CODES["not_found"]}
    assert wrong.status_code == 405
    assert wrong.json()["error"] == "method_not_allowed"
    v3_wrong = await client.get("/v3/sync/courses/upload")
    assert v3_wrong.json() == {"detail": "Method Not Allowed"}
    assert wrong.headers["allow"] == v3_wrong.headers["allow"]
    assert (await client.get("/v3/no-such-route")).json() == {"detail": "Not Found"}


async def test_a_validation_error_is_invalid_request_with_its_fields(client) -> None:
    session = await login(client)
    body = {"local_status": "done"}
    v4 = await client.patch(
        "/v4/sync/assignments/9001/override", headers=bearer(session), json=body
    )
    v3 = await client.patch(
        "/v3/sync/assignments/9001/override", headers=bearer(session), json=body
    )

    assert (v4.status_code, v3.status_code) == (422, 422)
    assert v4.json() == {
        "error": "invalid_request",
        "message": CODES["invalid_request"],
        "fields": v3.json()["detail"],
    }
    assert {"type", "loc", "msg"} == set(v4.json()["fields"][0])


async def test_auth_errors_keep_their_code_and_header(client) -> None:
    v4 = await client.get("/v4/sync/revision")
    v3 = await client.get("/v3/sync/revision")
    bad = await client.get("/v4/sync/revision", headers={"Authorization": "Bearer nope"})

    assert v4.json() == {"error": "missing_bearer_token", "message": CODES["missing_bearer_token"]}
    assert v3.json() == {"detail": "missing_bearer_token"}
    assert v4.headers["www-authenticate"] == v3.headers["www-authenticate"] == "Bearer"
    assert (bad.status_code, bad.json()["error"]) == (401, "invalid_access_token")


async def test_v3_prose_details_become_codes_on_v4(client) -> None:
    session = await login(client)
    cases = [
        ("DELETE", "/devices/no-such-device", 404, "device not found", "device_not_found"),
        ("GET", "/settings/appearance", 404, "settings document not found",
         "settings_document_not_found"),
        ("GET", "/settings/bogus", 422, "unknown namespace 'bogus'", "unknown_namespace"),
        ("GET", "/bulletins/999999", 404, "bulletin not found", "bulletin_not_found"),
    ]
    for method, path, status, prose, code in cases:
        v3 = await client.request(method, f"/v3{path}", headers=bearer(session))
        v4 = await client.request(method, f"/v4{path}", headers=bearer(session))
        assert (v3.status_code, v3.json()) == (status, {"detail": prose}), path
        assert (v4.status_code, v4.json()) == (
            status, {"error": code, "message": CODES[code]}
        ), path


async def test_a_conflict_keeps_its_fields_beside_the_code(client) -> None:
    session = await login(client)
    body = {"document": {"theme": "dark"}, "base_revision": None}
    assert (await client.put("/v4/settings/appearance", headers=bearer(session), json=body)).status_code == 200

    v4 = await client.put("/v4/settings/appearance", headers=bearer(session), json=body)
    v3 = await client.put("/v3/settings/appearance", headers=bearer(session), json=body)

    assert (v4.status_code, v3.status_code) == (409, 409)
    assert v3.json().keys() == {"error", "namespace", "server"}
    assert v4.json() == {**v3.json(), "message": CODES["settings_conflict"]}


async def test_both_versions_share_one_refresh_budget(client) -> None:
    answers = [
        await client.post(f"/{version}/auth/refresh", json={"refresh_token": "nope"})
        for version in ("v3",) * 5 + ("v4",) * 6
    ]

    assert [a.status_code for a in answers[:10]] == [401] * 10
    assert answers[9].json()["error"] == "invalid_refresh_token"
    assert answers[10].status_code == 429
    assert answers[10].json() == {
        "error": "too_many_refresh_attempts",
        "message": CODES["too_many_refresh_attempts"],
    }


async def test_an_unhandled_error_is_internal_error_json(client) -> None:
    async def fail() -> None:
        raise RuntimeError("boom")

    client.app.add_api_route("/v4/test-failure", fail)
    client.app.add_api_route("/v3/test-failure", fail)
    transport = ASGITransport(app=client.app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as unraised:
        v4 = await unraised.get("/v4/test-failure")
        v3 = await unraised.get("/v3/test-failure")

    assert v4.status_code == 500
    assert v4.json() == {"error": "internal_error", "message": CODES["internal_error"]}
    assert CLOCK_HEADER in v4.headers
    assert (v3.status_code, v3.text) == (500, "Internal Server Error")


async def test_the_envelope_maps_every_v3_detail() -> None:
    assert envelope(409, {"detail": "clock_ahead"}) == {
        "error": "clock_ahead", "message": CODES["clock_ahead"],
    }
    assert envelope(404, {"detail": "subscription not found"})["error"] == "subscription_not_found"
    assert envelope(429, {"detail": "too_many_login_attempts"})["error"] == "too_many_login_attempts"
    assert envelope(503, None) == {"error": "service_unavailable", "message": "Service Unavailable"}
    assert envelope(500, {"detail": "Something broke"})["error"] == "internal_error"
    # A prose detail with no code of its own keeps its words as the message.
    assert envelope(400, {"detail": "Some prose"}) == {"error": "bad_request", "message": "Some prose"}
    assert envelope(409, {"error": "x", "message": "kept", "extra": 1}) == {
        "error": "x", "message": "kept", "extra": 1,
    }


# --- Protocol 2 implied ---------------------------------------------------------


async def test_the_deletes_and_holiday_put_need_an_hlc_whatever_the_header(client) -> None:
    session = await login(client)
    holiday_id = await seed_holiday(client)
    headers = {**bearer(session), "X-TigerSync-Protocol": "1"}
    writes = [
        ("DELETE", f"/sync/courses/{KEY}", None),
        ("DELETE", "/sync/courses?semester=1141", None),
        ("PUT", f"/sync/holiday-overrides/{holiday_id}", {"notify": True}),
    ]
    for method, path, body in writes:
        v4 = await client.request(method, f"/v4{path}", headers=headers, json=body)
        assert (v4.status_code, v4.json()["error"]) == (422, "hlc_required"), path
        v3 = await client.request(method, f"/v3{path}", headers=bearer(session), json=body)
        assert v3.status_code == 200, path


async def test_a_stamp_ahead_of_the_clock_is_clock_ahead(client) -> None:
    session = await login(client)
    ahead = to_clock(datetime.now(UTC) + timedelta(minutes=5))
    response = await client.delete(
        f"/v4/sync/courses/{KEY}", headers=bearer(session), params={"hlc": ahead}
    )
    assert response.status_code == 409
    assert response.json() == {"error": "clock_ahead", "message": CODES["clock_ahead"]}
