"""Moodle webservice clients used by the server-side sync worker.

Same pattern as `server/auth/moodle.py`: Protocol interfaces, Http
implementations with an injectable `httpx` transport so tests stay
offline via MockTransport.

Error taxonomy is the load-bearing part — the executor's retry policy
hangs off it (security review 1.4):

* `MoodleTokenInvalid`  → cached token died; try ONE SSO re-obtain.
* `SsoAuthFailed`       → auth-class: credentials rejected. NEVER retried.
* `SsoUnavailable`      → network-class: SSO never evaluated the password,
                          safe to retry with backoff.
* `MoodleRateLimited`   → school API pushback; retry with backoff.
* `MoodleUnreachable`   → transient transport/parse error; retry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import httpx
import structlog

logger = structlog.get_logger(__name__)

_WS_PATH = "/webservice/rest/server.php"
_TOKEN_PATH = "/login/token.php"
_TOKEN_SERVICE = "moodle_mobile_app"


class MoodleClientError(Exception):
    """Base for all Moodle/SSO client failures."""


class MoodleTokenInvalid(MoodleClientError):
    pass


class MoodleRateLimited(MoodleClientError):
    pass


class MoodleUnreachable(MoodleClientError):
    pass


class SsoAuthFailed(MoodleClientError):
    pass


class SsoUnavailable(MoodleClientError):
    pass


@dataclass(frozen=True)
class FetchedAssignment:
    moodle_course_id: int
    moodle_assignment_id: int
    course_name: str | None
    title: str
    due_at: datetime | None
    cutoff_at: datetime | None
    allow_from_at: datetime | None
    moodle_url: str | None
    intro_html: str | None


@dataclass(frozen=True)
class ObtainedToken:
    token: str
    private_token: str | None


@dataclass(frozen=True)
class FetchedCourse:
    moodle_course_id: int
    short_name: str | None
    full_name: str
    category_id: int | None
    enrolled_user_count: int | None


class AssignmentFetcher(Protocol):
    async def fetch_assignments(self, *, token: str) -> list[FetchedAssignment]: ...


class CourseFetcher(Protocol):
    async def fetch_courses(self, *, token: str) -> list[FetchedCourse]: ...


class TokenObtainer(Protocol):
    async def obtain_token(
        self, *, username: str, password: str
    ) -> ObtainedToken: ...


def _ts(value: object) -> datetime | None:
    """Moodle uses unix seconds with 0 meaning 'unset'."""
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return datetime.fromtimestamp(value, tz=UTC)


class HttpAssignmentFetcher:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._transport = transport

    async def fetch_assignments(self, *, token: str) -> list[FetchedAssignment]:
        params = {
            "wstoken": token,
            "wsfunction": "mod_assign_get_assignments",
            "moodlewsrestformat": "json",
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.get(
                    f"{self._base_url}{_WS_PATH}", params=params
                )
        except httpx.HTTPError as exc:
            # Never log the token.
            logger.warning("syncjobs.moodle.unreachable", error=str(exc)[:200])
            raise MoodleUnreachable(str(exc)[:200]) from exc

        if response.status_code == 429:
            raise MoodleRateLimited("http_429")
        try:
            body = response.json()
        except ValueError as exc:
            raise MoodleUnreachable("invalid_json") from exc
        if not isinstance(body, dict) or response.status_code >= 400:
            raise MoodleUnreachable(f"http_{response.status_code}")
        if "exception" in body:
            # Moodle reports webservice errors as 200 + exception payload.
            errorcode = str(body.get("errorcode", ""))
            if errorcode == "invalidtoken":
                raise MoodleTokenInvalid(errorcode)
            raise MoodleUnreachable(errorcode or "moodle_exception")

        fetched: list[FetchedAssignment] = []
        for course in body.get("courses", []):
            course_id = course.get("id")
            if not isinstance(course_id, int):
                continue
            course_name = course.get("fullname") or None
            for item in course.get("assignments", []):
                assignment_id = item.get("id")
                if not isinstance(assignment_id, int):
                    continue
                cmid = item.get("cmid")
                fetched.append(
                    FetchedAssignment(
                        moodle_course_id=course_id,
                        moodle_assignment_id=assignment_id,
                        course_name=course_name,
                        title=str(item.get("name") or ""),
                        due_at=_ts(item.get("duedate")),
                        cutoff_at=_ts(item.get("cutoffdate")),
                        allow_from_at=_ts(item.get("allowsubmissionsfromdate")),
                        moodle_url=(
                            f"{self._base_url}/mod/assign/view.php?id={cmid}"
                            if isinstance(cmid, int)
                            else None
                        ),
                        intro_html=item.get("intro") or None,
                    )
                )
        return fetched


class HttpCourseFetcher:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._transport = transport

    async def fetch_courses(self, *, token: str) -> list[FetchedCourse]:
        params = {
            "wstoken": token,
            "wsfunction": "core_enrol_get_users_courses",
            "moodlewsrestformat": "json",
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.get(
                    f"{self._base_url}{_WS_PATH}", params=params
                )
        except httpx.HTTPError as exc:
            logger.warning("syncjobs.moodle.courses_unreachable", error=str(exc)[:200])
            raise MoodleUnreachable(str(exc)[:200]) from exc

        if response.status_code == 429:
            raise MoodleRateLimited("http_429")
        try:
            body = response.json()
        except ValueError as exc:
            raise MoodleUnreachable("invalid_json") from exc
        if isinstance(body, dict) and "exception" in body:
            errorcode = str(body.get("errorcode", ""))
            if errorcode == "invalidtoken":
                raise MoodleTokenInvalid(errorcode)
            raise MoodleUnreachable(errorcode or "moodle_exception")
        if not isinstance(body, list):
            raise MoodleUnreachable(f"unexpected_shape_{type(body).__name__}")

        return [
            FetchedCourse(
                moodle_course_id=c["id"],
                short_name=c.get("shortname"),
                full_name=str(c.get("fullname") or c.get("shortname") or ""),
                category_id=c.get("categoryid"),
                enrolled_user_count=c.get("enrolledusercount"),
            )
            for c in body
            if isinstance(c.get("id"), int)
        ]


class HttpTokenObtainer:
    """Re-obtains a Moodle webservice token from a username/password via
    `/login/token.php`. Called by the sync worker only when the cached
    token has expired — and at most once per run (iron rule lives in
    `server/syncjobs/credentials.py`)."""

    def __init__(
        self,
        base_url: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._transport = transport

    async def obtain_token(
        self, *, username: str, password: str
    ) -> ObtainedToken:
        data = {
            "username": username,
            "password": password,
            "service": _TOKEN_SERVICE,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.post(
                    f"{self._base_url}{_TOKEN_PATH}", data=data
                )
        except httpx.HTTPError as exc:
            logger.warning("syncjobs.sso.unreachable", error=str(exc)[:200])
            raise SsoUnavailable(str(exc)[:200]) from exc

        if response.status_code >= 500 or response.status_code == 429:
            raise SsoUnavailable(f"http_{response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise SsoUnavailable("invalid_json") from exc
        if not isinstance(body, dict):
            raise SsoUnavailable("invalid_body")

        token = body.get("token")
        if isinstance(token, str) and token:
            return ObtainedToken(
                token=token, private_token=body.get("privatetoken") or None
            )
        errorcode = str(body.get("errorcode", ""))
        if errorcode == "invalidlogin":
            logger.warning("syncjobs.sso.auth_failed", username=username)
            raise SsoAuthFailed(errorcode)
        raise SsoUnavailable(errorcode or f"http_{response.status_code}")
