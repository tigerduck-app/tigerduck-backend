"""Dev-only test push page.

Fires synthetic alert / Live Activity pushes via the backend's
`/v2/_debug/*` surface so an operator can verify the APNs path without
waiting for a real bulletin or class slot to elapse. None of the
underlying endpoints write to the bulletins / scheduled_pushes /
live_activity_update_tokens tables — pass / fail here reflects the
APNs transport itself.

Gated three ways:
  * `_require_dev` here returns 404 in prod.
  * The matching backend endpoints (server/routes/debug.py) return 404
    in prod via their own `_require_dev`.
  * The nav link in `_base.html` is wrapped in `{% if env == "development" %}`.
"""
from __future__ import annotations

from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from ..status import BACKEND_INTERNAL_URL

router = APIRouter(prefix="/test")


def _require_dev(request: Request) -> None:
    if request.app.state.settings.env != "development":
        raise HTTPException(status_code=404, detail="not found")


async def _backend_call(
    request: Request,
    method: str,
    path: str,
    *,
    timeout_s: float = 15.0,
    **kwargs,
) -> httpx.Response:
    """Forward to `tigerduck-internal:40000/v2/_debug<path>` with the
    shared-secret header attached. The portal sits on the same docker
    network as the backend so this is always reachable regardless of
    public DNS / port publishing."""
    secret = request.app.state.settings.api_shared_secret
    headers = kwargs.pop("headers", {})
    if secret:
        headers["X-Push-Token"] = secret
    url = f"{BACKEND_INTERNAL_URL}/v2/_debug{path}"
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        return await client.request(method, url, headers=headers, **kwargs)


def _proxy_json(r: httpx.Response) -> JSONResponse:
    """Mirror the backend's status + JSON body so the page's JS gets the
    full error detail rather than a generic 500 from the portal."""
    try:
        body = r.json() if r.content else {}
    except ValueError:
        body = {"detail": r.text or f"HTTP {r.status_code}"}
    return JSONResponse(status_code=r.status_code, content=body)


async def _proxy_post(
    request: Request, path: str, payload: BaseModel
) -> JSONResponse:
    """POST to the backend's `/v2/_debug<path>` and mirror the response.

    Wraps `_backend_call` with httpx error handling so a network hiccup
    or backend restart returns a structured 502 to the page's JS rather
    than an unhandled exception that becomes a FastAPI 500 + traceback.
    """
    try:
        r = await _backend_call(
            request, "POST", path, json=payload.model_dump()
        )
    except httpx.HTTPError as exc:
        return JSONResponse(
            status_code=502,
            content={
                "detail": f"backend call failed: {type(exc).__name__}: {exc}"
            },
        )
    return _proxy_json(r)


@router.get("", response_class=HTMLResponse, dependencies=[Depends(_require_dev)])
async def page(
    request: Request,
    tab: Literal["announcement", "apple"] = "announcement",
) -> HTMLResponse:
    devices: list[dict] = []
    devices_error: str | None = None
    if tab == "apple":
        try:
            r = await _backend_call(request, "GET", "/devices")
            if r.status_code == 200:
                devices = r.json()
            else:
                devices_error = f"backend HTTP {r.status_code}: {r.text[:200]}"
        except httpx.HTTPError as exc:
            devices_error = f"{type(exc).__name__}: {exc}"
    return request.app.state.templates.TemplateResponse(
        request,
        "test_push.html",
        {
            "tab": tab,
            "devices": devices,
            "devices_error": devices_error,
        },
    )


# ---------------------------------------------------------------------------
# JSON proxies driven by the page's fetch() submissions.
# Pydantic models here mirror the backend's request schemas so we 422
# early (before the cross-container HTTP hop) on obvious payload bugs.
# ---------------------------------------------------------------------------


class _AlertSubmission(BaseModel):
    # Length limits mirror the backend's _SendAlertRequest so we 422
    # locally instead of paying a cross-container round-trip for an
    # obviously-bad payload.
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=2000)
    device_ids: list[str] | None = None


@router.post(
    "/api/send_alert",
    response_class=JSONResponse,
    dependencies=[Depends(_require_dev)],
)
async def api_send_alert(
    payload: _AlertSubmission, request: Request
) -> JSONResponse:
    return await _proxy_post(request, "/send_alert", payload)


class _LiveActivitySubmission(BaseModel):
    # Length limits mirror the backend's _SendLiveActivityRequest.
    device_id: str = Field(min_length=1)
    scenario: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=120)
    subtitle: str = Field(default="", max_length=200)
    location_text: str = Field(default="", max_length=120)
    countdown_target_iso: str | None = None
    source_id: str = Field(default="debug-test", min_length=1)


@router.post(
    "/api/send_live_activity",
    response_class=JSONResponse,
    dependencies=[Depends(_require_dev)],
)
async def api_send_live_activity(
    payload: _LiveActivitySubmission, request: Request
) -> JSONResponse:
    return await _proxy_post(request, "/send_live_activity", payload)


class _EndLiveActivitySubmission(BaseModel):
    activity_id: str = Field(min_length=1)


@router.post(
    "/api/end_live_activity",
    response_class=JSONResponse,
    dependencies=[Depends(_require_dev)],
)
async def api_end_live_activity(
    payload: _EndLiveActivitySubmission, request: Request
) -> JSONResponse:
    return await _proxy_post(request, "/end_live_activity", payload)
