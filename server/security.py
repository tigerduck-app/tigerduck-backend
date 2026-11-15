"""Shared-secret auth for the admin endpoints.

Checked via the `X-Push-Token` header. If `settings.api_shared_secret` is
unset (empty string) the dependency is a no-op — convenient for local dev
and the test suite. Production must set ``TIGERDUCK_API_SHARED_SECRET``.
"""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Header, HTTPException, Request, status

# The operator the portal's Cloudflare Access check verified, which the portal
# sends to an admin endpoint that records it in admin_audit_log.
AdminActor = Annotated[str, Header(alias="X-Admin-Actor", min_length=1, max_length=320)]


async def require_shared_secret(
    request: Request,
    x_push_token: str | None = Header(default=None, alias="X-Push-Token"),
) -> None:
    expected: str = request.app.state.settings.api_shared_secret
    if not expected:
        # In production the secret MUST be configured (validated at startup
        # via Settings.validate_production_secrets). If somehow reached at
        # runtime with an empty secret, reject rather than silently allowing.
        if request.app.state.settings.env == "production":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="shared secret not configured",
            )
        return  # auth disabled (dev/test default)
    if x_push_token is None or not secrets.compare_digest(x_push_token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing X-Push-Token",
        )
