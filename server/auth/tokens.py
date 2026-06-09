"""Access-JWT and refresh-token primitives.

Access tokens are stateless HS256 JWTs (15 min default): claims are
`sub` (user UUID), `sid` (auth_sessions.id), `did` (user_devices.id or
absent), `token_use="access"`. The `token_use` claim stops any other JWT
we might mint later from being replayed as an access token.

Refresh tokens are opaque 384-bit random strings; the DB stores only their
HMAC-SHA-256 (keyed separately from the JWT secret, so neither secret leak
compromises the other token class).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt as pyjwt

_ALGORITHM = "HS256"


class InvalidAccessToken(Exception):
    """Signature/expiry/claim validation failed."""


@dataclass(frozen=True)
class AccessClaims:
    user_id: str
    session_id: int
    device_id: str | None


def issue_access_token(
    secret: str,
    *,
    user_id: str,
    session_id: int,
    device_id: str | None,
    ttl_seconds: int,
    now: datetime | None = None,
) -> str:
    now = now or datetime.now(UTC)
    payload: dict = {
        "sub": user_id,
        "sid": session_id,
        "token_use": "access",
        "iat": now,
        "exp": now + timedelta(seconds=ttl_seconds),
    }
    if device_id is not None:
        payload["did"] = device_id
    return pyjwt.encode(payload, secret, algorithm=_ALGORITHM)


def decode_access_token(secret: str, token: str) -> AccessClaims:
    try:
        payload = pyjwt.decode(
            token,
            secret,
            algorithms=[_ALGORITHM],
            options={"require": ["sub", "sid", "exp"]},
        )
    except pyjwt.PyJWTError as exc:
        raise InvalidAccessToken(str(exc)) from exc
    if payload.get("token_use") != "access":
        raise InvalidAccessToken("token_use claim missing or not 'access'")
    return AccessClaims(
        user_id=str(payload["sub"]),
        session_id=int(payload["sid"]),
        device_id=str(payload["did"]) if "did" in payload else None,
    )


def generate_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_refresh_token(hmac_key: str, token: str) -> str:
    return hmac.new(hmac_key.encode(), token.encode(), hashlib.sha256).hexdigest()
