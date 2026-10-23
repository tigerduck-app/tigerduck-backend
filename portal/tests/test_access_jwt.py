"""Every /api request needs a valid Cloudflare Access JWT outside development.

Tokens are signed here with a local RSA key, and the verifier's key-set
fetch is stubbed to return that key, so no test talks to Cloudflare.
"""
from __future__ import annotations

import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request

from app.access import build_verifier, require_access
from app.config import Settings
from app.routes import status

pytestmark = pytest.mark.anyio

TEAM = "team.cloudflareaccess.com"
AUD = "portal-aud-tag"
KID = "test-key"
JWT_HEADER = "Cf-Access-Jwt-Assertion"

SIGNING_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwks() -> dict:
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(SIGNING_KEY.public_key()))
    jwk.update(kid=KID, alg="RS256", use="sig")
    return {"keys": [jwk]}


def _token(
    *,
    key=SIGNING_KEY,
    aud: str = AUD,
    iss: str = f"https://{TEAM}",
    expires_in: int = 300,
    email: str = "ops@example.com",
) -> str:
    claims = {
        "aud": [aud],
        "iss": iss,
        "exp": int(time.time()) + expires_in,
        "email": email,
    }
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": KID})


def _app(env: str = "production", team: str = TEAM, aud: str = AUD) -> FastAPI:
    app = FastAPI()
    settings = Settings.model_construct(
        env=env, cf_access_team_domain=team, cf_access_aud=aud
    )
    app.state.settings = settings
    verifier = build_verifier(settings)
    if verifier is not None:
        verifier.jwks.fetch_data = _jwks
    app.state.access_verifier = verifier
    app.middleware("http")(require_access)
    app.include_router(status.router)
    app.include_router(status.liveness_router)

    @app.post("/api/echo")
    async def echo(request: Request) -> dict:
        return {"actor": request.state.actor}

    return app


async def _call(
    app: FastAPI, method: str, path: str, headers: dict[str, str] | None = None
) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://portal") as c:
        return await c.request(method, path, headers=headers)


def _auth(token: str, **extra: str) -> dict[str, str]:
    return {JWT_HEADER: token, "Sec-Fetch-Site": "same-origin", **extra}


async def test_a_valid_token_names_the_actor() -> None:
    r = await _call(_app(), "POST", "/api/echo", _auth(_token()))

    assert r.status_code == 200
    assert r.json() == {"actor": "ops@example.com"}


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(_token(aud="another-app"), id="wrong-aud"),
        pytest.param(_token(iss="https://evil.cloudflareaccess.com"), id="wrong-iss"),
        pytest.param(_token(expires_in=-60), id="expired"),
        pytest.param(_token(key=OTHER_KEY), id="bad-signature"),
        pytest.param(_token(email=""), id="no-email"),
        pytest.param("not-a-jwt", id="garbage"),
    ],
)
async def test_a_bad_token_is_refused(token: str) -> None:
    r = await _call(_app(), "GET", "/api/env", {JWT_HEADER: token})

    assert r.status_code == 403


async def test_a_missing_token_is_refused() -> None:
    # The email header alone was trusted before; it proves nothing.
    r = await _call(
        _app(), "GET", "/api/env", {"Cf-Access-Authenticated-User-Email": "ops@example.com"}
    )

    assert r.status_code == 403


@pytest.mark.parametrize("team,aud", [("", AUD), (TEAM, ""), ("", "")])
async def test_production_without_access_config_refuses_everything(
    team: str, aud: str
) -> None:
    r = await _call(_app(team=team, aud=aud), "GET", "/api/env", {JWT_HEADER: _token()})

    assert r.status_code == 403


async def test_development_skips_the_check_and_calls_the_actor_dev() -> None:
    app = _app(env="development", team="", aud="")

    echo = await _call(app, "POST", "/api/echo")
    env = await _call(app, "GET", "/api/env")

    assert echo.json() == {"actor": "dev"}
    assert env.json()["access"] is None


async def test_health_needs_no_token() -> None:
    r = await _call(_app(), "GET", "/health")

    assert r.status_code == 200


async def test_env_shows_the_verified_identity_not_the_email_header() -> None:
    r = await _call(
        _app(),
        "GET",
        "/api/env",
        _auth(_token(), **{"Cf-Access-Authenticated-User-Email": "spoof@example.com"}),
    )

    assert r.status_code == 200
    assert r.json()["access"] == {
        "email": "ops@example.com",
        "logout_url": "/cdn-cgi/access/logout",
    }
    # The body names the operator, so no shared cache may keep it.
    assert r.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
async def test_a_cross_site_write_is_refused(site: str) -> None:
    r = await _call(
        _app(), "POST", "/api/echo", {JWT_HEADER: _token(), "Sec-Fetch-Site": site}
    )

    assert r.status_code == 403


async def test_a_cross_site_read_is_allowed() -> None:
    r = await _call(
        _app(), "GET", "/api/env", {JWT_HEADER: _token(), "Sec-Fetch-Site": "cross-site"}
    )

    assert r.status_code == 200


async def test_without_sec_fetch_site_a_foreign_origin_write_is_refused() -> None:
    r = await _call(
        _app(),
        "POST",
        "/api/echo",
        {JWT_HEADER: _token(), "Origin": "https://evil.example"},
    )

    assert r.status_code == 403


async def test_without_sec_fetch_site_a_same_origin_write_is_allowed() -> None:
    r = await _call(
        _app(), "POST", "/api/echo", {JWT_HEADER: _token(), "Origin": "http://portal"}
    )

    assert r.status_code == 200


async def test_development_still_refuses_cross_site_writes() -> None:
    r = await _call(
        _app(env="development"), "POST", "/api/echo", {"Sec-Fetch-Site": "cross-site"}
    )

    assert r.status_code == 403
