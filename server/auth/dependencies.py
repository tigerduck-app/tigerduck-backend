"""FastAPI dependency: authenticate /v3 requests via Bearer access JWT.

Besides verifying the JWT itself, this checks the backing auth_session is
not revoked — so logout, device deletion, and refresh-reuse detection cut
access immediately instead of "within 15 minutes when the JWT expires".
One PK lookup per authenticated request; the session object is shared with
the route handler via FastAPI's per-request dependency cache.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from server.auth.models import AuthSession
from server.auth.tokens import InvalidAccessToken, decode_access_token
from server.db import SessionDep


@dataclass(frozen=True)
class CurrentAuth:
    user_id: uuid.UUID
    session_id: int
    device_id: uuid.UUID | None


async def require_user(
    request: Request,
    session: SessionDep,
    authorization: str | None = Header(default=None),
) -> CurrentAuth:
    secret: str = request.app.state.settings.auth_jwt_secret
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="auth_not_configured",
        )
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing_bearer_token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.removeprefix("Bearer ").strip()
    try:
        claims = decode_access_token(secret, token)
        user_id = uuid.UUID(claims.user_id)
        device_id = uuid.UUID(claims.device_id) if claims.device_id else None
    except (InvalidAccessToken, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_access_token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    auth_session = await session.get(AuthSession, claims.session_id)
    if auth_session is None or auth_session.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="session_revoked",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return CurrentAuth(
        user_id=user_id, session_id=claims.session_id, device_id=device_id
    )


CurrentAuthDep = Annotated[CurrentAuth, Depends(require_user)]
