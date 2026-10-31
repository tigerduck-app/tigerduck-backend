"""What `/v4` answers differently from `/v3` on every route: it implies
TigerSync protocol 2 (`ClockProtocolDep`), and every error it sends is the
envelope `{"error", "message", ...}` (docs/api-v4.md).

The routes keep `/v3`'s `HTTPException` details. The envelope is built from
the error body afterwards, for `/v4` paths only, so `/v3` bodies stay as
they are.
"""

from __future__ import annotations

import json
from http import HTTPStatus

from starlette.requests import Request
from starlette.responses import Response

#: Every code a `/v4` error carries, with the English message sent beside it.
CODES = {
    "device_id_required": "This session carries no device id.",
    "moodle_token_required": "Sign-in needs a Moodle token.",
    "missing_bearer_token": "No bearer token was sent.",
    "invalid_access_token": "The access token is invalid or has expired.",
    "session_revoked": "The session has ended; sign in again.",
    "session_user_mismatch": "The session belongs to another account; sign in again.",
    "invalid_refresh_token": "The refresh token is invalid.",
    "refresh_token_expired": "The refresh token has expired.",
    "moodle_verification_failed": "Moodle did not accept the token.",
    "account_suspended": "The account is suspended.",
    "not_found": "No such route.",
    "method_not_allowed": "The route does not take this method.",
    "device_not_found": "No such device.",
    "course_not_found": "No such course.",
    "assignment_not_found": "No such assignment.",
    "holiday_not_found": "No such holiday.",
    "settings_document_not_found": "No such settings document.",
    "subscription_not_found": "No such subscription.",
    "bulletin_not_found": "No such bulletin.",
    "term_not_found": "No such term.",
    "course_catalog_not_found": "No course catalog for this term.",
    "clock_ahead": "The hlc is ahead of the server's clock.",
    "login_conflict_retry": "Another sign-in won a race; try again.",
    "settings_conflict": "The settings document changed since base_revision.",
    "subscription_conflict": "The subscription changed since base_revision.",
    "sync_revision_expired": "The change log no longer reaches since_revision; sync in full.",
    "api_retired": "This API version is retired; update the app.",
    "app_version_retired": "This app version is no longer supported; update the app.",
    "invalid_request": "The request did not validate.",
    "hlc_required": "This write needs an hlc.",
    "add_not_in_courses": "Every add must name a course in courses.",
    "activity_id_mismatch": "activity_id is not scenario::source_id.",
    "unknown_namespace": "No such settings namespace.",
    "too_many_login_attempts": "Too many sign-in attempts; try again later.",
    "too_many_refresh_attempts": "Too many refresh attempts; try again later.",
    "too_many_credential_updates": "Too many credential updates; try again later.",
    "too_many_registration_attempts": "Too many registrations; try again later.",
    "internal_error": "Internal server error.",
    "auth_not_configured": "Sign-in is not configured on this server.",
    "moodle_unreachable": "Moodle did not answer.",
}

# `/v3` details that are prose, and the code `/v4` sends for each. Starlette's
# own 404 and 405 details become codes from their status phrase (`_code`).
_PROSE = {
    "device not found": "device_not_found",
    "settings document not found": "settings_document_not_found",
    "subscription not found": "subscription_not_found",
    "bulletin not found": "bulletin_not_found",
    "too many refresh attempts, try again later": "too_many_refresh_attempts",
}


def under(path: str, prefix: str) -> bool:
    """`path` is `prefix` or below it; an empty prefix holds nothing."""
    prefix = prefix.rstrip("/")
    return bool(prefix) and (path == prefix or path.startswith(prefix + "/"))


def speaks_v4(request: Request) -> bool:
    return under(request.url.path, request.app.state.settings.api_v4_base_path)


def _code(status: int, detail: object) -> str:
    if isinstance(detail, str):
        if detail.startswith("unknown namespace "):
            return "unknown_namespace"
        if detail in _PROSE:
            return _PROSE[detail]
        if detail.isidentifier() and detail.islower():
            return detail
    if status == 500:
        return "internal_error"
    return HTTPStatus(status).phrase.lower().replace(" ", "_").replace("-", "_")


def envelope(status: int, content: object) -> dict:
    """The `/v4` body for an error whose `/v3` body is `content` (None when
    it had no JSON one). A body that already names its `error`, such as
    `settings_conflict`, keeps its fields beside it."""
    if isinstance(content, dict) and isinstance(content.get("error"), str):
        code = content["error"]
        fields = {k: v for k, v in content.items() if k != "error"}
    elif isinstance(content, dict) and isinstance(content.get("detail"), list):
        code, fields = "invalid_request", {"fields": content["detail"]}
    else:
        detail = content.get("detail") if isinstance(content, dict) else None
        code, fields = _code(status, detail), {}
        if code not in CODES and isinstance(detail, str):
            fields["message"] = detail
    message = fields.pop("message", None) or CODES.get(code) or HTTPStatus(status).phrase
    return {"error": code, "message": message, **fields}


def error_response(status: int, content: object, headers=()) -> Response:
    # ASCII JSON, as for the `/v3` 422 (`main.py`): a lone surrogate in a
    # `loc` key would fail to encode as UTF-8.
    answer = Response(
        json.dumps(envelope(status, content)), status_code=status, media_type="application/json"
    )
    for name, value in headers:
        if name not in ("content-length", "content-type"):
            answer.headers.append(name, value)
    return answer


async def enveloped(response: Response) -> Response:
    """`response`, an error a `/v4` route sent, with its body in the envelope."""
    body = b"".join([chunk async for chunk in response.body_iterator])
    try:
        content = json.loads(body)
    except ValueError:
        content = None
    return error_response(response.status_code, content, response.headers.items())
