"""Apple test-push proxies (dev-only, JSON-only).

Fires synthetic Live Activity / single-device alert pushes via the
backend's `/v2/_debug/*` surface so an operator can verify the APNs
path without waiting for a real bulletin or class slot to elapse.
None of the underlying endpoints write to the
scheduled_pushes / live_activity_update_tokens tables — pass/fail
here reflects the APNs transport itself.

Gated three ways:
  * `_require_dev` here returns 404 in prod.
  * The matching backend endpoints (server/routes/debug.py) return 404
    in prod via their own `_require_dev`.
  * The React nav hides this page when env != "development".
"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..status import BACKEND_INTERNAL_URL

router = APIRouter(prefix="/api/test")


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
    shared-secret header attached. Always reachable over the docker
    network regardless of public DNS / port publishing."""
    secret = request.app.state.settings.api_shared_secret
    headers = kwargs.pop("headers", {})
    if secret:
        headers["X-Push-Token"] = secret
    url = f"{BACKEND_INTERNAL_URL}/v2/_debug{path}"
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        return await client.request(method, url, headers=headers, **kwargs)


def _proxy_json(r: httpx.Response) -> JSONResponse:
    try:
        body = r.json() if r.content else {}
    except ValueError:
        body = {"detail": r.text or f"HTTP {r.status_code}"}
    return JSONResponse(status_code=r.status_code, content=body)


async def _proxy_post(
    request: Request, path: str, payload: BaseModel
) -> JSONResponse:
    try:
        r = await _backend_call(request, "POST", path, json=payload.model_dump())
    except httpx.HTTPError as exc:
        return JSONResponse(
            status_code=502,
            content={"detail": f"backend call failed: {type(exc).__name__}: {exc}"},
        )
    return _proxy_json(r)


@router.get("/devices", dependencies=[Depends(_require_dev)])
async def devices(request: Request) -> JSONResponse:
    try:
        r = await _backend_call(request, "GET", "/devices")
    except httpx.HTTPError as exc:
        return JSONResponse(
            status_code=502,
            content={"detail": f"{type(exc).__name__}: {exc}"},
        )
    if r.status_code != 200:
        return JSONResponse(
            status_code=r.status_code,
            content={"detail": f"backend HTTP {r.status_code}: {r.text[:200]}"},
        )
    return JSONResponse(content=r.json())


class _AlertSubmission(BaseModel):
    # Length limits mirror the backend's _SendAlertRequest so we 422
    # locally instead of paying the cross-container round-trip.
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=2000)
    device_ids: list[str] | None = None


@router.post("/send_alert", dependencies=[Depends(_require_dev)])
async def send_alert(payload: _AlertSubmission, request: Request) -> JSONResponse:
    return await _proxy_post(request, "/send_alert", payload)


class _LiveActivitySubmission(BaseModel):
    device_id: str = Field(min_length=1)
    scenario: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=120)
    subtitle: str = Field(default="", max_length=200)
    location_text: str = Field(default="", max_length=120)
    countdown_target_iso: str | None = None
    source_id: str = Field(default="debug-test", min_length=1)


@router.post("/send_live_activity", dependencies=[Depends(_require_dev)])
async def send_live_activity(
    payload: _LiveActivitySubmission, request: Request
) -> JSONResponse:
    return await _proxy_post(request, "/send_live_activity", payload)


class _EndLiveActivitySubmission(BaseModel):
    activity_id: str = Field(min_length=1)


@router.post("/end_live_activity", dependencies=[Depends(_require_dev)])
async def end_live_activity(
    payload: _EndLiveActivitySubmission, request: Request
) -> JSONResponse:
    return await _proxy_post(request, "/end_live_activity", payload)
