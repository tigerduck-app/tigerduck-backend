"""/v3 auth endpoints: login, refresh, logout, credential refresh."""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, HTTPException, Request, status
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

# Per-IP rate limit for /auth/refresh — separate from login_limiter so
# the two windows don't interfere with each other.
_REFRESH_MAX_ATTEMPTS = 10
_REFRESH_WINDOW_SECONDS = 60

# Per-user rate limit for PATCH /auth/credentials — every call makes a live
# Moodle verification, so an unthrottled loop would amplify traffic against
# NTUST Moodle. Legitimate traffic is one call per app foreground.
_CREDENTIALS_MAX_ATTEMPTS = 10
_CREDENTIALS_WINDOW_SECONDS = 300


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
    did_sso_rate_limit = False

    if not moodle_token and payload.password:
        ip_key = f"login:ip:{_client_ip(request)}"
        sid_key = f"login:sid:{payload.student_id.lower()}"
        if not state.login_limiter.allow(ip_key) or not state.login_limiter.allow(sid_key):
            from fastapi import HTTPException
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="too_many_login_attempts",
            )
        state.login_limiter.record(ip_key)
        state.login_limiter.record(sid_key)
        did_sso_rate_limit = True
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
        skip_rate_limit=did_sso_rate_limit,
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
    ip = _client_ip(request)
    limiter = request.app.state.refresh_limiter
    if not limiter.allow(ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many refresh attempts, try again later",
        )
    limiter.record(ip)
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
    Verifies the token against Moodle, then updates the encrypted credential
    blob and auto-revives disabled sync jobs so a token-invalid outage heals
    the moment the user opens the app."""
    limiter = request.app.state.credentials_limiter
    limit_key = f"credentials:{auth.user_id}"
    if not limiter.allow(limit_key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too_many_credential_updates",
        )
    limiter.record(limit_key)

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

    # Same live check as login: without it, a stale token would keep
    # flipping the account back to `active` and re-arming disabled sync
    # jobs with attempts=0, defeating the worker's back-off. The endpoint
    # retries on the next app foreground, so a failed check is a no-op
    # rather than an error.
    verify = await request.app.state.moodle_verifier.verify(
        token=payload.moodle_token, student_id=account.external_user_id
    )
    if not verify.ok:
        logger.info(
            "auth.credentials.moodle_rejected",
            user_id=str(auth.user_id),
            error=verify.error,
        )
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
