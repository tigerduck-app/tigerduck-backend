"""FastAPI dependency: authenticate /v3 requests via Bearer access JWT."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from server.auth.tokens import InvalidAccessToken, decode_access_token


@dataclass(frozen=True)
class CurrentAuth:
    user_id: uuid.UUID
    session_id: int
    device_id: uuid.UUID | None


async def require_user(
    request: Request,
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
    return CurrentAuth(
        user_id=user_id, session_id=claims.session_id, device_id=device_id
    )


CurrentAuthDep = Annotated[CurrentAuth, Depends(require_user)]
