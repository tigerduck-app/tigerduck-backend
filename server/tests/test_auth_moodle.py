"""Unit tests for the Moodle token verifier (mocked transport)."""

from __future__ import annotations

import json

import httpx

from server.auth.moodle import HttpMoodleVerifier, StaticMoodleVerifier


def make_verifier(handler) -> HttpMoodleVerifier:
    transport = httpx.MockTransport(handler)
    return HttpMoodleVerifier(
        base_url="https://moodle.example.edu",
        timeout_seconds=5.0,
        transport=transport,
    )


async def test_valid_token_matching_username() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/webservice/rest/server.php"
        assert request.url.params["wsfunction"] == "core_webservice_get_site_info"
        assert request.url.params["wstoken"] == "tok123"
        return httpx.Response(200, json={"username": "b11015000", "userid": 1})

    result = await make_verifier(handler).verify(
        token="tok123", student_id="B11015000"
    )
    assert result.ok is True
    assert result.username == "b11015000"


async def test_invalid_token_returns_moodle_exception() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "exception": "moodle_exception",
                "errorcode": "invalidtoken",
                "message": "Invalid token",
            },
        )

    result = await make_verifier(handler).verify(token="bad", student_id="B11015000")
    assert result.ok is False
    assert result.error == "invalid_token"


async def test_username_mismatch_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"username": "someoneelse"})

    result = await make_verifier(handler).verify(
        token="tok", student_id="B11015000"
    )
    assert result.ok is False
    assert result.error == "username_mismatch"


async def test_http_error_maps_to_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    result = await make_verifier(handler).verify(
        token="tok", student_id="B11015000"
    )
    assert result.ok is False
    assert result.error == "unreachable"


async def test_non_json_body_maps_to_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>maintenance</html>")

    result = await make_verifier(handler).verify(
        token="tok", student_id="B11015000"
    )
    assert result.ok is False
    assert result.error == "unreachable"


async def test_static_verifier_returns_configured_result() -> None:
    from server.auth.moodle import MoodleVerifyResult

    static = StaticMoodleVerifier(MoodleVerifyResult(ok=True, username="x"))
    result = await static.verify(token="anything", student_id="x")
    assert result.ok is True


async def test_token_never_logged_in_result() -> None:
    # The result dataclass must not carry the token back (it could end up in
    # structured logs).
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"username": "b1"})

    result = await make_verifier(handler).verify(token="secret-tok", student_id="b1")
    assert "secret-tok" not in json.dumps(result.__dict__)
