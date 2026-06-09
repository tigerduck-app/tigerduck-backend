"""/v3 auth endpoints: login (refresh/logout added in later tasks)."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Request

from server.auth import service
from server.auth.schemas import LoginRequest, LoginResponse, UserOut
from server.db import SessionDep

router = APIRouter(prefix="/auth", tags=["auth"])
logger = structlog.get_logger(__name__)


def _client_ip(request: Request) -> str:
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
