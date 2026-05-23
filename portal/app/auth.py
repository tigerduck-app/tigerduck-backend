"""Portal auth — currently a no-op.

The portal trusts whatever is in front of it (Cloudflare Zero Trust in
prod, nothing in dev) and does not enforce its own gate. This module
keeps `require_admin` as a FastAPI dependency so route signatures stay
stable, but it never raises — every request is allowed and audit-log
rows are written with a synthetic actor email.

To re-introduce a gate later, change `require_admin` to look up
`current_user_email(request)` in the `admins` SQLite table and 403 on
miss. The Cloudflare header name is still threaded through `Settings`
for that future use.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from .config import Settings


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def current_user_email(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> str | None:
    """Return the Cloudflare-verified email when present, else None.

    Not used as a gate today — included so the status page can show
    'served behind cloudflared as <email>' when the header arrives."""
    return request.headers.get(settings.cf_access_email_header)


def require_admin(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> str:
    """No-op gate. Returns the CF-verified email if present, else a
    synthetic 'portal' actor — used as the audit-log actor."""
    return (
        request.headers.get(settings.cf_access_email_header)
        or "portal"
    )
