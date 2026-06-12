"""/v3 auth endpoints: login, refresh, logout."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Request, status

from server.auth.dependencies import CurrentAuthDep

from server.auth import service
from server.auth.schemas import (
    LoginRequest,
    LoginResponse,
    RefreshRequest,
    RefreshResponse,
    UserOut,
)
from server.db import SessionDep

router = APIRouter(prefix="/auth", tags=["auth"])
logger = structlog.get_logger(__name__)


def _client_ip(request: Request) -> str:
    if request.app.state.settings.auth_trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            # First hop = original client when the trusted proxy appends.
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest, request: Request, session: SessionDep
) -> LoginResponse:
    state = request.app.state
    result = await service.login(
        session,
        state.settings,
        state.moodle_verifier,
        state.login_limiter,
        state.credential_cipher,
        student_id=payload.student_id,
        password=payload.password,
        moodle_token=payload.moodle_token,
        moodle_private_token=payload.moodle_private_token,
        device_info=payload.device_info,
        client_ip=_client_ip(request),
    )
    return LoginResponse(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        expires_in=result.expires_in,
        user=UserOut(
            id=str(result.user.id),
            student_id=result.user.student_id,
            display_name=result.user.display_name,
        ),
        device_id=str(result.device.id),
    )


@router.post("/refresh", response_model=RefreshResponse)
async def refresh(
    payload: RefreshRequest, request: Request, session: SessionDep
) -> RefreshResponse:
    result = await service.refresh(
        session,
        request.app.state.settings,
        refresh_token=payload.refresh_token,
    )
    return RefreshResponse(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        expires_in=result.expires_in,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(auth: CurrentAuthDep, session: SessionDep) -> None:
    """Revoke the calling session. Idempotent — repeated logouts 204."""
    await service.logout(session, session_id=auth.session_id)
