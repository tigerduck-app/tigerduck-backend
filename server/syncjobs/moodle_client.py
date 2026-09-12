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
import re
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


# --- submission-probe failure policy ---------------------------------------
#
# `HttpAssignmentFetcher.fetch_submission_status` runs one probe per
# assignment inside an `asyncio.TaskGroup`, and a TaskGroup cancels every
# sibling task the moment one member raises. So "may this exception leave a
# probe?" *is* that function's blast-radius policy, and it is stated once,
# here, instead of being spread across inline `except` clauses that have
# drifted apart three times already.

# The three the executor's retry policy actually branches on
# (`executor.py::_execute_job`: `MoodleTokenInvalid` is converted to
# `CredentialInvalid` by a wrapper at each Moodle call site, and
# `_execute_job` has its own `except MoodleRateLimited` and
# `except MoodleUnreachable` handlers). Raised from inside a probe they
# describe the credential or the endpoint rather than one assignment's
# data, so every remaining probe is doomed anyway: these escalate out of
# the TaskGroup and the `except*` router re-raises them unwrapped.
#
# This is an ordered table, not a bare tuple, and the router below is a
# loop over it rather than three hard-coded branches. That is deliberate:
# the previous shape hard-coded a branch per type with a fallback that
# raised `MoodleUnreachable`, so adding a fourth member here -- a one-word
# edit, in a tuple whose own comment promised the router "re-raises them
# unwrapped" -- silently converted that fourth type into `MoodleUnreachable`
# at the caller (task-1-fix-4-rereview.md M1). Adding a member now means
# adding a `(type, code)` row, which the router handles by construction.
#
# Order is priority, highest first, for a batch that hits more than one: a
# dead token makes everything else moot (we must stop using the credential
# either way) and only it triggers re-auth, so it wins; a 429 is a specific
# instruction from the server, so it outranks the generic transient.
#
# The code beside each type is what gets re-raised -- a short stable string
# rather than the original message, same as `MoodleRateLimited("http_429")`
# always has: `_execute_job`'s `except MoodleUnreachable` handler writes
# `str(exc)` into `last_error`, and a fixed code there is greppable and
# cannot carry anything sensitive.
#
# Deliberately absent: `SsoAuthFailed` / `SsoUnavailable`, which describe a
# password login this code path never performs (only `SsoTokenClient` raises
# them, and only `routes/auth.py` handles them), and the bare
# `MoodleClientError` base. `_execute_job` has no branch for any of the
# three, so escalating one would cancel the whole batch to report something
# the caller cannot act on -- and `SsoAuthFailed` specifically means
# "credentials rejected, NEVER retried", a verdict a single assignment's
# probe has no business delivering. They count as unexpected and skip one
# assignment like anything else unrecognised.
_PROBE_ESCALATIONS: tuple[tuple[type[MoodleClientError], str], ...] = (
    (MoodleTokenInvalid, "invalidtoken"),
    (MoodleRateLimited, "http_429"),
    (MoodleUnreachable, "submission_probe_unreachable"),
)

# Derived, never hand-written: the `except`/`except*` clauses and the
# router must agree about the membership by construction. Pinned by
# `test_every_escalation_reaches_the_caller_as_its_own_type`, which
# parametrizes over the table so a new row is a new case automatically.
_PROBE_ESCALATES = tuple(kind for kind, _ in _PROBE_ESCALATIONS)

# Ordinary "this one assignment's request or data is broken": skip the
# assignment, keep the batch. `OverflowError` is *not* a `ValueError` (it
# descends from `ArithmeticError`), and `_ts` raises it for a `timemodified`
# too large for the platform's `time_t` -- a size JSON permits.
#
# Nothing here may overlap `_PROBE_ESCALATES`, or the skip clause would
# swallow an escalation before the outer clause ever sees it. Pinned by
# `test_skip_tuple_can_never_swallow_an_escalation`.
_PROBE_SKIPS = (httpx.HTTPError, ValueError, OverflowError)


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


_REDACTED = "<token-redacted>"

# Any `wstoken=...` still embedded in text, whatever its value. The token we
# hold is scrubbed by value; this catches a differently-shaped one (a
# truncation, a stale token from another frame) so the guarantee does not
# depend on an exact string match.
_WSTOKEN_IN_TEXT = re.compile(r"(wstoken=)[^&\s'\"]*", re.IGNORECASE)


def _scrub(text: str, token: str) -> str:
    return _WSTOKEN_IN_TEXT.sub(r"\1" + _REDACTED, text.replace(token, _REDACTED))


def _without_token(exc: BaseException, token: str) -> BaseException:
    """Strip `token` out of every message reachable from `exc`, in place.

    Logging with `exc_info` renders a traceback, and a traceback's last
    line is `<Type>: <str(exc)>` -- plus the same for every exception in
    the `__cause__`/`__context__` chain, and for any `__notes__`. So
    `exc_info` is only token-safe as long as no exception *message* on
    this path ever carries the token. Nothing reachable builds such a
    message today (`probe` raises only fixed codes, and the httpx
    exceptions that embed a URL are all `httpx.HTTPError`, i.e.
    `_PROBE_SKIPS`, which never reach here) -- but that is a margin that
    depends on a third-party library's message formatting, and the
    never-log-a-token rule is absolute. The frames that log these
    exceptions are the ones that know the token's value -- the residual
    handler in `probe`, and the executor's handler around the whole
    fetch -- so it is closed there rather than assumed.

    Mutating the exception is safe *there specifically*: both handlers log
    and return, so `exc` is discarded immediately after and is never
    re-raised or seen by a caller.

    Frame locals are a separate axis and are not touched -- the
    production chain renders source lines, not locals, which is what
    `test_unexpected_probe_error_logs_a_traceback_without_the_token`
    pins against the real `logging_setup.configure` chain.
    """
    if not token:
        return exc
    pending: list[BaseException | None] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        try:
            current.args = tuple(
                _scrub(a, token) if isinstance(a, str) else a for a in current.args
            )
            notes = getattr(current, "__notes__", None)
            if notes:
                current.__notes__ = [
                    _scrub(n, token) if isinstance(n, str) else n for n in notes
                ]
            if isinstance(current, BaseExceptionGroup):
                pending.extend(current.exceptions)
        except Exception:
            # Some exceptions build `str()` from attributes rather than
            # `args` and reject the assignment. Scrub what we can; the
            # caller's own `except Exception: pass` is the backstop.
            pass
        pending.append(current.__cause__)
        pending.append(current.__context__)
    return exc


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
        assignment in the batch.

        `_PROBE_ESCALATES` is the closed list of exceptions that *do* stop
        the batch, because each describes the credential or the endpoint
        rather than one assignment: `MoodleTokenInvalid` (every subsequent
        call would fail too, and the executor must disable the job),
        `MoodleRateLimited` and `MoodleUnreachable` (same contract as
        `fetch_assignments` — the executor's backoff, not this best-effort
        probe, decides what happens next). All three arrive at the caller
        unwrapped, never inside an `ExceptionGroup`; if more than one kind
        occurs in a batch, token beats 429 beats unreachable and the
        discarded ones stay reachable through `__cause__`.

        `BaseException` is the one thing that still leaves uncaught,
        deliberately: `KeyboardInterrupt`, `SystemExit` and `GeneratorExit`
        raised inside a probe must tear the batch down rather than be
        quietly converted into a skipped assignment. They do not all arrive
        the same way, though, and an earlier version of this docstring said
        they did: `asyncio.TaskGroup` re-raises `SystemExit` and
        `KeyboardInterrupt` *bare* (`_is_base_error` covers exactly those
        two, and `raise self._base_error` bypasses the group), while
        `GeneratorExit` comes out inside a `BaseExceptionGroup` like
        anything else. Cancelling *this* call while probes are in flight
        also still raises a plain `CancelledError`, not a group.

        One exception to that, which the code cannot change without
        weakening cancellation: an `asyncio.CancelledError` raised *inside
        one probe* is `TaskGroup` semantics — that child is `cancelled()`
        rather than failed, so the group records no error and the
        assignment is simply absent from the result, with nothing logged.
        Nothing in this codebase cancels an individual probe, and the
        operationally important case (outer cancellation, above) is
        correct; this is written down because the comments here used to
        claim a guarantee one step stronger than asyncio delivers.
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
                    # This `try` wraps `probe`'s *whole body*, not just the
                    # statements that look like they can raise. A TaskGroup
                    # cancels every sibling task as soon as one member
                    # raises, so anything leaving this function costs the
                    # entire batch and reaches the caller as a bare
                    # `ExceptionGroup` matching no `except*` clause below.
                    # Guarding only "the parts that can raise" is exactly
                    # what let that shape survive three fix rounds: the
                    # result store sat after the inner guard, and the
                    # handlers' own `logger` calls sat inside it. Only two
                    # exits are permitted from here -- a normal return, or
                    # one of `_PROBE_ESCALATES`. See task-1-fix-4-report.md.
                    try:
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
                                # Parsing is part of the same guarded region
                                # as the request: a malformed `timemodified`
                                # is exactly "this assignment's data is bad",
                                # the same failure class as a transport error
                                # (task-1-fix-3-brief.md C1 -- this call used
                                # to sit after the guard, so `_ts`'s
                                # ValueError/OverflowError escaped `probe`).
                                parsed = _parse_submission_status(assignment_id, body)
                            except _PROBE_SKIPS as exc:
                                # One assignment's failure is that
                                # assignment's alone -- never the token, so
                                # only the type name is logged. The two
                                # escalations raised just above are not in
                                # this tuple and cannot be (pinned by
                                # `test_skip_tuple_can_never_swallow_an_escalation`),
                                # so they fall through to the outer clause.
                                logger.warning(
                                    "syncjobs.moodle.submission_probe_failed",
                                    error=type(exc).__name__,
                                )
                                return
                            if parsed is not None:
                                results[assignment_id] = parsed
                    except _PROBE_ESCALATES:
                        # The credential or the endpoint is the problem, not
                        # this assignment: every sibling probe would fail the
                        # same way, so let it reach the `except*` router.
                        raise
                    except Exception as exc:
                        # The residual. Whatever a probe raises that no
                        # clause named is still one assignment's problem, so
                        # it skips that assignment instead of cancelling its
                        # siblings -- but it is not a shape anyone
                        # anticipated either, so it is logged at `error`
                        # rather than folding into routine probe noise, and
                        # with a traceback: a bare type name cannot tell you
                        # *where* inside `probe` a RuntimeError came from,
                        # which is the whole point of the signal.
                        #
                        # Two separate leak axes, both closed, both pinned:
                        # frame *locals* are never rendered by the chains
                        # `logging_setup.configure` builds (pinned by
                        # `test_unexpected_probe_error_logs_a_traceback_without_the_token`,
                        # which runs the real `configure`), and exception
                        # *messages* -- which `exc_info` does render, for
                        # the whole `__cause__`/`__context__` chain -- are
                        # scrubbed by `_without_token` (pinned by
                        # `test_a_token_in_the_exception_message_is_not_logged`).
                        #
                        # `BaseException` is deliberately NOT caught here:
                        # `KeyboardInterrupt`, `SystemExit` and
                        # `GeneratorExit` raised inside a probe tear the
                        # batch down rather than become a skipped
                        # assignment. Only `GeneratorExit` arrives as a
                        # `BaseExceptionGroup`; `asyncio.TaskGroup`
                        # re-raises the other two bare. Cancelling
                        # `fetch_submission_status` itself still
                        # propagates a plain `CancelledError`. Note the one
                        # case this does *not* buy: an
                        # `asyncio.CancelledError` raised *inside a single
                        # probe* is `TaskGroup` semantics, not ours -- that
                        # child counts as cancelled rather than failed, so
                        # the group never sees an error and the assignment
                        # is simply absent from the result, silently.
                        # Nothing in this codebase cancels an individual
                        # probe; if something ever does, it must not rely on
                        # this clause to notice.
                        try:
                            logger.error(
                                "syncjobs.moodle.submission_probe_unexpected_error",
                                error=type(exc).__name__,
                                exc_info=_without_token(exc, token),
                            )
                        except Exception:
                            # The last resort must not itself fail the
                            # batch. structlog writes through
                            # `PrintLoggerFactory` (logging_setup.py), i.e.
                            # a plain write to stdout, and an `OSError` on a
                            # broken pipe here would hand the caller back
                            # the very ExceptionGroup this clause exists to
                            # prevent. Losing one log line beats losing
                            # every other assignment in the batch.
                            pass

                try:
                    async with asyncio.TaskGroup() as tg:
                        for assignment_id in assignment_ids:
                            tg.create_task(probe(assignment_id))
                except* _PROBE_ESCALATES as eg:
                    # Unwrapped: the executor matches on the exception
                    # itself, not an ExceptionGroup. These used to be
                    # separate `except*` arms, but `except*` runs every
                    # matching clause -- a batch with one dead-token probe
                    # and one rate-limited probe fired both and the two
                    # raises were recombined into a bare ExceptionGroup
                    # that matched nothing downstream. So: one clause, and
                    # one loop over `_PROBE_ESCALATIONS`, which is ordered
                    # by priority and is the same table `_PROBE_ESCALATES`
                    # is derived from -- every member that can reach this
                    # clause therefore has a row here, and cannot be
                    # silently retyped into whatever the last branch
                    # happened to raise.
                    #
                    # `subgroup()`, not `eg.exceptions` -- groups can nest
                    # and `.exceptions` only sees the top level.
                    #
                    # The re-raise carries the table's short stable code
                    # rather than the original message; see the table. The
                    # discarded detail is not lost -- `from eg` keeps the
                    # whole group reachable as `exc.__cause__`.
                    for kind, code in _PROBE_ESCALATIONS:
                        if eg.subgroup(kind) is not None:
                            raise kind(code) from eg
                    # Unreachable: `except*` only matched because some
                    # member of `_PROBE_ESCALATES` -- i.e. some row above
                    # -- is in the group. Kept so a future edit that breaks
                    # that invariant still raises something the executor
                    # can act on instead of falling through and returning a
                    # half-filled dict.
                    raise MoodleUnreachable("submission_probe_unreachable") from eg
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
