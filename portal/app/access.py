"""The Cloudflare Access session a request arrived with, if any.

The portal has no sign-in of its own. In production it sits behind a
Cloudflare Access application, and Access adds the signed-in user's email
to every request it lets through. A request that reached the portal
directly (dev, LAN) has no such header and no Access session to end.

The email is for display only. Nothing here authorises anything: a client
that bypasses Access could set the header itself, and all it would get is
a wrong label next to a sign-out link.
"""
from __future__ import annotations

from fastapi import Request

EMAIL_HEADER = "Cf-Access-Authenticated-User-Email"

# Served by Cloudflare's edge on every Access-protected hostname, so the
# request never reaches the portal. It clears the app's authorization
# cookie and revokes the user's Access session across all apps.
LOGOUT_PATH = "/cdn-cgi/access/logout"


def access_session(request: Request) -> dict[str, str] | None:
    """`{email, logout_url}` when Access fronted the request, else None."""
    email = request.headers.get(EMAIL_HEADER, "").strip()
    if not email:
        return None
    return {"email": email, "logout_url": LOGOUT_PATH}
