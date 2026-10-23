"""Who is calling the portal's API, proven by Cloudflare Access.

In production the portal sits behind a Cloudflare Access application, which
adds a signed JWT (`Cf-Access-Jwt-Assertion`) to every request it lets
through. Every `/api/*` request must carry one that verifies against the
team's signing keys, issuer and the application's AUD tag, the same check
Grafana makes; anything else gets 403. Network position proves nothing: other
containers on proxy-net and LAN hosts can reach the portal without Access.

`TIGERDUCK_ENV=development` skips the check and calls everyone `dev`.

Writes are also refused when the browser says they come from another site,
so a page elsewhere cannot ride an operator's Access cookie.
"""
from __future__ import annotations

import logging
from urllib.parse import urlsplit

import jwt
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)

JWT_HEADER = "Cf-Access-Jwt-Assertion"
DEV_ACTOR = "dev"

# Served by Cloudflare's edge on every Access-protected hostname, so the
# request never reaches the portal. It clears the app's authorization
# cookie and revokes the user's Access session across all apps.
LOGOUT_PATH = "/cdn-cgi/access/logout"

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_SAME_SITE_FETCH = frozenset({"same-origin", "none"})
_JWKS_TIMEOUT_SECONDS = 5
# A token with a made-up kid may force a key refetch at most this often.
_UNKNOWN_KID_REFETCH_SECONDS = 60


class AccessVerifier:
    """Checks a Cloudflare Access JWT and returns the signed-in email."""

    def __init__(self, team_domain: str, audience: str) -> None:
        self.issuer = f"https://{team_domain}"
        self.audience = audience
        # Caches keys 5 minutes; an unknown kid refetches, so rotation works.
        self.jwks = jwt.PyJWKClient(
            f"{self.issuer}/cdn-cgi/access/certs",
            timeout=_JWKS_TIMEOUT_SECONDS,
            cooldown_duration=_UNKNOWN_KID_REFETCH_SECONDS,
        )

    def verify(self, token: str) -> str:
        key = self.jwks.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            audience=self.audience,
            issuer=self.issuer,
            options={"require": ["exp", "iss", "aud"]},
        )
        email = claims.get("email")
        if not isinstance(email, str) or not email:
            raise jwt.InvalidTokenError("token has no email claim")
        return email


def build_verifier(settings) -> AccessVerifier | None:
    """The production verifier, or None (every API request then gets 403)."""
    if settings.env == "development":
        return None
    if not settings.cf_access_team_domain or not settings.cf_access_aud:
        logger.error(
            "TIGERDUCK_CF_ACCESS_TEAM_DOMAIN or TIGERDUCK_CF_ACCESS_AUD is unset: "
            "the portal API refuses every request until both are set"
        )
        return None
    return AccessVerifier(settings.cf_access_team_domain, settings.cf_access_aud)


def _forbidden(detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=403)


def _is_cross_site(request: Request) -> bool:
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        return site not in _SAME_SITE_FETCH
    origin = request.headers.get("origin")
    if origin is None:
        # Not a browser, so no ambient Access cookie to abuse.
        return False
    return urlsplit(origin).netloc != request.headers.get("host")


async def require_access(request: Request, call_next):
    """HTTP middleware: sets `request.state.actor` for every `/api/*` request."""
    if not request.url.path.startswith("/api/"):
        return await call_next(request)

    if request.app.state.settings.env == "development":
        request.state.actor = DEV_ACTOR
    else:
        verifier = request.app.state.access_verifier
        token = request.headers.get(JWT_HEADER)
        if verifier is None or not token:
            return _forbidden("Cloudflare Access token required")
        try:
            # PyJWKClient fetches the key set with blocking urllib.
            request.state.actor = await run_in_threadpool(verifier.verify, token)
        except jwt.PyJWTError as exc:
            logger.warning("Access token rejected: %s", exc)
            return _forbidden("invalid Cloudflare Access token")

    if request.method not in _SAFE_METHODS and _is_cross_site(request):
        return _forbidden("cross-site request refused")
    return await call_next(request)


def access_session(request: Request) -> dict[str, str] | None:
    """`{email, logout_url}` for a verified Access user, None in development."""
    actor = getattr(request.state, "actor", None)
    if actor is None or actor == DEV_ACTOR:
        return None
    return {"email": actor, "logout_url": LOGOUT_PATH}
