"""Login-time Moodle token verification.

The app does the NTUST SSO dance itself (SSO rate-limits per IP, so the
backend must not proxy it) and hands us a Moodle webservice token. Before
trusting the login we make ONE lightweight Moodle call to confirm the
token works and belongs to the claimed student id.

`MoodleVerifier` is a Protocol so routes depend on the interface;
`create_app` wires `HttpMoodleVerifier`, tests inject `StaticMoodleVerifier`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import httpx
import structlog

logger = structlog.get_logger(__name__)

_WS_PATH = "/webservice/rest/server.php"

# What Moodle answers a token it does not take with: an unknown one, and an
# expired one (webservice_access_exception). Any other error, such as
# maintenance mode, is Moodle failing and says nothing about the token.
_TOKEN_ERRORCODES = frozenset({"invalidtoken", "accessexception"})

# The length of `users.display_name`.
_DISPLAY_NAME_MAX = 128


def display_name_from(fullname: str | None) -> str | None:
    """The name in Moodle's full name, as `users.display_name` stores it;
    None when blank. NTUST spells a full name "student ID @ name", so the
    name is the part after the first `@`, as the apps show it; without one,
    or with nothing after it, the full name is the name."""
    whole = (fullname or "").strip()
    name = whole.partition("@")[2].strip() or whole
    return name[:_DISPLAY_NAME_MAX].rstrip() or None


@dataclass(frozen=True)
class MoodleVerifyResult:
    ok: bool
    username: str | None = None
    # "invalid_token" | "username_mismatch" | "unreachable"
    error: str | None = None
    fullname: str | None = field(default=None, repr=False)


class MoodleVerifier(Protocol):
    async def verify(self, *, token: str, student_id: str) -> MoodleVerifyResult: ...


class HttpMoodleVerifier:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._transport = transport

    async def verify(self, *, token: str, student_id: str) -> MoodleVerifyResult:
        params = {
            "wstoken": token,
            "wsfunction": "core_webservice_get_site_info",
            "moodlewsrestformat": "json",
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.get(
                    f"{self._base_url}{_WS_PATH}", params=params
                )
                body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # Never log the token itself.
            logger.warning("moodle.verify.unreachable", error=str(exc)[:200])
            return MoodleVerifyResult(ok=False, error="unreachable")

        if not isinstance(body, dict) or response.status_code >= 400:
            return MoodleVerifyResult(ok=False, error="unreachable")
        if "exception" in body:
            # Moodle reports errors as 200 + exception payload.
            errorcode = str(body.get("errorcode", ""))
            if errorcode in _TOKEN_ERRORCODES:
                return MoodleVerifyResult(ok=False, error="invalid_token")
            logger.warning("moodle.verify.moodle_error", errorcode=errorcode[:64])
            return MoodleVerifyResult(ok=False, error="unreachable")

        username = str(body.get("username", ""))
        if username.lower() != student_id.lower():
            logger.warning(
                "moodle.verify.username_mismatch",
                student_id=student_id,
                moodle_username=username,
            )
            return MoodleVerifyResult(ok=False, error="username_mismatch")
        fullname = body.get("fullname")
        return MoodleVerifyResult(
            ok=True,
            username=username,
            fullname=fullname if isinstance(fullname, str) else None,
        )


class StaticMoodleVerifier:
    """Returns a fixed result — for tests and local development."""

    def __init__(self, result: MoodleVerifyResult) -> None:
        self._result = result

    async def verify(self, *, token: str, student_id: str) -> MoodleVerifyResult:
        return self._result
