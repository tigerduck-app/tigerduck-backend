"""Unit tests for the Moodle token verifier (mocked transport)."""

from __future__ import annotations

import json

import httpx
import pytest

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


def moodle_error(errorcode: str | None) -> httpx.Response:
    body = {"exception": "moodle_exception", "message": "..."}
    if errorcode is not None:
        body["errorcode"] = errorcode
    return httpx.Response(200, json=body)


@pytest.mark.parametrize(
    "errorcode",
    [
        "invalidtoken",
        # An expired token, as webservice_access_exception reports it.
        "accessexception",
    ],
)
async def test_a_refused_token_maps_to_invalid_token(errorcode) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return moodle_error(errorcode)

    result = await make_verifier(handler).verify(token="bad", student_id="B11015000")
    assert result.ok is False
    assert result.error == "invalid_token"


@pytest.mark.parametrize("errorcode", ["sitemaintenance", "dmlreadexception", None])
async def test_any_other_moodle_error_maps_to_unreachable(errorcode) -> None:
    """Moodle in maintenance, or failing, answers 200 with an exception
    that says nothing about the token."""

    def handler(request: httpx.Request) -> httpx.Response:
        return moodle_error(errorcode)

    result = await make_verifier(handler).verify(token="tok", student_id="B11015000")
    assert result.ok is False
    assert result.error == "unreachable"


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


async def test_the_site_info_full_name_is_carried() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"username": "b1", "userid": 1, "fullname": "Test Student"}
        )

    result = await make_verifier(handler).verify(token="tok", student_id="b1")
    assert result.ok is True
    assert result.fullname == "Test Student"


async def test_a_site_info_without_a_full_name_carries_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"username": "b1", "userid": 1})

    result = await make_verifier(handler).verify(token="tok", student_id="b1")
    assert result.ok is True
    assert result.fullname is None
