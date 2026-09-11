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

import asyncio
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


@dataclass(frozen=True)
class FetchedSubmission:
    moodle_assignment_id: int
    is_submitted: bool
    submitted_at: datetime | None


class AssignmentFetcher(Protocol):
    async def fetch_assignments(self, *, token: str) -> list[FetchedAssignment]: ...

    async def fetch_submission_status(
        self, *, token: str, assignment_ids: list[int], max_concurrency: int
    ) -> dict[int, FetchedSubmission]: ...


class SubmissionStatusFetcher(Protocol):
    async def fetch_submission_status(
        self, *, token: str, assignment_ids: list[int], max_concurrency: int
    ) -> dict[int, FetchedSubmission]: ...


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


async def _get_moodle_userid(
    client: httpx.AsyncClient, base_url: str, token: str
) -> int:
    params = {
        "wstoken": token,
        "wsfunction": "core_webservice_get_site_info",
        "moodlewsrestformat": "json",
    }
    response = await client.get(f"{base_url}{_WS_PATH}", params=params)
    if response.status_code == 429:
        raise MoodleRateLimited("http_429")
    body = response.json()
    if isinstance(body, dict) and "exception" in body:
        errorcode = str(body.get("errorcode", ""))
        if errorcode == "invalidtoken":
            raise MoodleTokenInvalid(errorcode)
        raise MoodleUnreachable(errorcode or "moodle_exception")
    if not isinstance(body, dict) or "userid" not in body:
        raise MoodleUnreachable("missing_userid_in_site_info")
    return int(body["userid"])


def _parse_submission_status(
    assignment_id: int, body: dict
) -> FetchedSubmission | None:
    """Read `lastattempt.submission` into a FetchedSubmission.

    Moodle omits `lastattempt` entirely for an assignment the user cannot
    submit to; that is not the same as "not submitted", so it yields None
    and the caller leaves the row alone.
    """
    last = body.get("lastattempt")
    if not isinstance(last, dict):
        return None
    submission = last.get("submission")
    if not isinstance(submission, dict):
        return None
    submitted = str(submission.get("status") or "") == "submitted"
    return FetchedSubmission(
        moodle_assignment_id=assignment_id,
        is_submitted=submitted,
        submitted_at=_ts(submission.get("timemodified")) if submitted else None,
    )


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

    async def fetch_submission_status(
        self, *, token: str, assignment_ids: list[int], max_concurrency: int
    ) -> dict[int, FetchedSubmission]:
        """Submission state for the given assignments, keyed by assignment id.

        Moodle exposes this one assignment at a time, so this is N requests
        behind a semaphore rather than a single call. Callers must pass a
        narrow list — see `submissions.select_assignment_ids`.

        Assignments whose probe fails are simply absent from the result;
        the caller leaves those rows as they were. That holds even for a
        probe failure of a kind nobody anticipated (logged at `error`, not
        `warning`, so it doesn't hide behind routine probe noise) — a
        programming error in parsing one assignment's data is still that
        assignment's problem alone, not grounds to fail every other
        assignment in the batch. An invalid token is the one exception: it
        means every subsequent call would fail too, so it propagates and
        lets the executor disable the job. A 429 stops the batch too and
        propagates as `MoodleRateLimited`, same contract as
        `fetch_assignments` — the executor's backoff, not this best-effort
        probe, decides what happens next.
        """
        if not assignment_ids:
            return {}

        results: dict[int, FetchedSubmission] = {}
        semaphore = asyncio.Semaphore(max(1, max_concurrency))

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                userid = await _get_moodle_userid(client, self._base_url, token)

                async def probe(assignment_id: int) -> None:
                    async with semaphore:
                        params = {
                            "wstoken": token,
                            "wsfunction": "mod_assign_get_submission_status",
                            "moodlewsrestformat": "json",
                            "assignid": assignment_id,
                            "userid": userid,
                        }
                        try:
                            response = await client.get(
                                f"{self._base_url}{_WS_PATH}", params=params
                            )
                            if response.status_code == 429:
                                raise MoodleRateLimited("http_429")
                            if response.status_code >= 400:
                                return
                            body = response.json()
                            if not isinstance(body, dict):
                                return
                            if "exception" in body:
                                if str(body.get("errorcode", "")) == "invalidtoken":
                                    raise MoodleTokenInvalid("invalidtoken")
                                return
                            # Parsing is part of the same guarded region as
                            # the request: a malformed `timemodified` is
                            # exactly "this assignment's data is bad", the
                            # same failure class as a transport error, and
                            # must be handled here rather than left to
                            # escape `probe` unmatched (task-1-fix-3-brief.md
                            # C1 -- `_parse_submission_status` used to be
                            # called after this try, so `_ts`'s ValueError/
                            # OverflowError left the TaskGroup as a bare
                            # ExceptionGroup that matched no except* clause
                            # below and no except clause at :329).
                            parsed = _parse_submission_status(assignment_id, body)
                        except (MoodleTokenInvalid, MoodleRateLimited):
                            # Deliberate escalations, not this assignment's
                            # problem alone -- let them reach the TaskGroup
                            # and the `except*` routing below, unlike
                            # everything caught by the two clauses after
                            # this one.
                            raise
                        except (httpx.HTTPError, ValueError, OverflowError) as exc:
                            # One assignment's failure is that assignment's
                            # alone -- never the token. OverflowError is
                            # *not* a ValueError (it descends from
                            # ArithmeticError, not ValueError), and `_ts`
                            # raises it for a `timemodified` too large for
                            # the platform's time_t -- an ordinary malformed
                            # value, not a contrived one, since JSON places
                            # no ceiling on integer size.
                            logger.warning(
                                "syncjobs.moodle.submission_probe_failed",
                                error=type(exc).__name__,
                            )
                            return
                        except Exception as exc:
                            # Anything else is still just this assignment's
                            # data or handling -- not grounds to fail the
                            # batch (the same contract as every branch
                            # above) -- but it is *not* a shape anticipated
                            # by the two clauses above either, so it is
                            # logged louder (error, not warning) instead of
                            # folding into routine probe noise. This is the
                            # deliberate close of the class of bug fixed
                            # piecemeal across three rounds: whatever a
                            # probe raises that no clause names must never
                            # reach the caller as a bare ExceptionGroup, and
                            # must not silently look identical to an
                            # expected, understood failure either. See
                            # task-1-fix-3-report.md for the reasoning.
                            logger.error(
                                "syncjobs.moodle.submission_probe_unexpected_error",
                                error=type(exc).__name__,
                            )
                            return
                        if parsed is not None:
                            results[assignment_id] = parsed

                try:
                    async with asyncio.TaskGroup() as tg:
                        for assignment_id in assignment_ids:
                            tg.create_task(probe(assignment_id))
                except* (MoodleTokenInvalid, MoodleRateLimited) as eg:
                    # Unwrapped: the executor matches on the exception
                    # itself, not an ExceptionGroup. Both clauses used to
                    # be separate `except*` arms, but `except*` runs every
                    # matching clause -- a batch with one dead-token probe
                    # and one rate-limited probe fired both and the two
                    # raises were recombined into a bare ExceptionGroup
                    # that matched nothing downstream. One clause, with an
                    # explicit priority: a dead token makes the rate limit
                    # moot (we must stop using the credential either way)
                    # and only the token error triggers re-auth, so it
                    # wins. `subgroup()`, not `eg.exceptions` -- groups
                    # can nest and `.exceptions` only sees the top level.
                    if eg.subgroup(MoodleTokenInvalid) is not None:
                        raise MoodleTokenInvalid("invalidtoken") from eg
                    raise MoodleRateLimited("http_429") from eg
        except (httpx.HTTPError, ValueError) as exc:
            # Never log the token.
            logger.warning(
                "syncjobs.moodle.submission_status_failed", error=str(exc)[:200]
            )
            return results

        return results


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
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                userid = await _get_moodle_userid(client, self._base_url, token)
                params = {
                    "wstoken": token,
                    "wsfunction": "core_enrol_get_users_courses",
                    "userid": str(userid),
                    "moodlewsrestformat": "json",
                }
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
