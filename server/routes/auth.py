"""/v3 auth endpoints: login, refresh, logout, credential refresh."""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Request, status
from sqlalchemy import select, update

from server.auth.crypto import CredentialCipher, build_credential_aad
from server.auth.dependencies import CurrentAuthDep
from server.auth.models import (
    CredentialStatus,
    ExternalAccount,
    ExternalAccountCredential,
)

from server.auth import service
from server.auth.schemas import (
    LoginRequest,
    LoginResponse,
    RefreshRequest,
    RefreshResponse,
    UpdateCredentialsRequest,
    UpdateCredentialsResponse,
    UserOut,
)
from server.db import SessionDep
from server.syncjobs.models import SyncJob, SyncJobStatus

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
    moodle_token = payload.moodle_token
    moodle_private_token = payload.moodle_private_token

    if not moodle_token and payload.password:
        ip_key = f"login:ip:{_client_ip(request)}"
        if not state.login_limiter.allow(ip_key):
            from fastapi import HTTPException
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="too_many_login_attempts",
            )
        state.login_limiter.record(ip_key)
        from server.syncjobs.moodle_client import SsoTokenClient, SsoAuthFailed, SsoUnavailable
        sso = SsoTokenClient(
            base_url=state.settings.moodle_base_url,
            timeout_seconds=15,
        )
        try:
            obtained = await sso.obtain_token(
                username=payload.student_id, password=payload.password
            )
            moodle_token = obtained.token
            moodle_private_token = obtained.private_token
        except SsoAuthFailed:
            from fastapi import HTTPException
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid_credentials",
            )
        except SsoUnavailable as exc:
            from fastapi import HTTPException
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"moodle_unavailable:{exc}",
            )

    result = await service.login(
        session,
        state.settings,
        state.moodle_verifier,
        state.login_limiter,
        state.credential_cipher,
        student_id=payload.student_id,
        moodle_token=moodle_token,
        moodle_private_token=moodle_private_token,
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


@router.patch("/credentials", response_model=UpdateCredentialsResponse)
async def update_credentials(
    payload: UpdateCredentialsRequest,
    auth: CurrentAuthDep,
    request: Request,
    session: SessionDep,
) -> UpdateCredentialsResponse:
    """Accept a fresh Moodle token from the app (called on every foreground).
    Updates the encrypted credential blob and auto-revives disabled sync jobs
    so a token-invalid outage heals the moment the user opens the app."""
    cipher: CredentialCipher | None = request.app.state.credential_cipher
    if cipher is None:
        return UpdateCredentialsResponse(updated=False)

    account = (
        await session.execute(
            select(ExternalAccount).where(ExternalAccount.user_id == auth.user_id)
        )
    ).scalar_one_or_none()
    if account is None:
        return UpdateCredentialsResponse(updated=False)

    credential = await session.get(ExternalAccountCredential, account.id)
    now = datetime.now(UTC)
    new_blob = {
        "token_cache": {
            "moodle_token": payload.moodle_token,
            "moodle_private_token": payload.moodle_private_token,
            "obtained_at": now.isoformat(),
        },
    }
    encrypted = cipher.encrypt(
        new_blob, build_credential_aad(account.id, account.provider)
    )
    if credential is None:
        credential = ExternalAccountCredential(
            external_account_id=account.id,
            encryption_key_id=encrypted.key_id,
            ciphertext=encrypted.ciphertext,
            nonce=encrypted.nonce,
            aad=encrypted.aad,
        )
        session.add(credential)
    else:
        credential.encryption_key_id = encrypted.key_id
        credential.ciphertext = encrypted.ciphertext
        credential.nonce = encrypted.nonce
        credential.aad = encrypted.aad
        credential.rotated_at = now

    was_invalid = account.credential_status != CredentialStatus.active.value
    account.credential_status = CredentialStatus.active.value
    account.last_auth_success_at = now
    account.last_auth_error = None

    if was_invalid:
        await session.execute(
            update(SyncJob)
            .where(
                SyncJob.user_id == auth.user_id,
                SyncJob.status == SyncJobStatus.disabled.value,
            )
            .values(
                status=SyncJobStatus.pending.value,
                run_after=now,
                attempts=0,
                locked_by=None,
                locked_at=None,
                last_error=None,
            )
        )

    return UpdateCredentialsResponse(updated=True)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(auth: CurrentAuthDep, session: SessionDep) -> None:
    """Revoke the calling session. Idempotent — repeated logouts 204."""
    await service.logout(session, session_id=auth.session_id)
