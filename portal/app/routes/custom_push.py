"""Custom push composer proxy.

Forwards `/api/custom-push/*` to the backend's `/v2/custom-push/*` over
the docker internal URL with `X-Push-Token` attached, so the SPA never
sees the shared secret.
"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..status import BACKEND_INTERNAL_URL

router = APIRouter(prefix="/api/custom-push")


async def _backend_request(
    request: Request,
    method: str,
    path: str,
    *,
    json: dict | None = None,
    params: dict | None = None,
    timeout_s: float = 15.0,
) -> httpx.Response:
    """Call `tigerduck-internal:40000/v2<path>` with X-Push-Token."""
    secret = request.app.state.settings.api_shared_secret
    headers: dict[str, str] = {}
    if secret:
        headers["X-Push-Token"] = secret
    url = f"{BACKEND_INTERNAL_URL}/v2{path}"
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        return await client.request(
            method, url, headers=headers, json=json, params=params
        )


def _proxy_json(r: httpx.Response) -> JSONResponse:
    """Mirror upstream status + body so the SPA gets the real detail."""
    try:
        body = r.json() if r.content else {}
    except ValueError:
        body = {"detail": r.text or f"HTTP {r.status_code}"}
    return JSONResponse(status_code=r.status_code, content=body)


def _proxy_error(exc: httpx.HTTPError) -> JSONResponse:
    return JSONResponse(
        status_code=502,
        content={"detail": f"backend call failed: {type(exc).__name__}: {exc}"},
    )


@router.post("/preview")
async def preview(request: Request) -> JSONResponse:
    body = await request.json()
    try:
        r = await _backend_request(
            request, "POST", "/custom-push/preview", json=body
        )
    except httpx.HTTPError as exc:
        return _proxy_error(exc)
    return _proxy_json(r)


@router.post("")
async def send(request: Request) -> JSONResponse:
    body = await request.json()
    try:
        r = await _backend_request(request, "POST", "/custom-push", json=body)
    except httpx.HTTPError as exc:
        return _proxy_error(exc)
    return _proxy_json(r)


@router.get("/recent")
async def recent(request: Request) -> JSONResponse:
    limit = request.query_params.get("limit", "30")
    try:
        r = await _backend_request(
            request, "GET", "/custom-push/recent", params={"limit": limit}
        )
    except httpx.HTTPError as exc:
        return _proxy_error(exc)
    return _proxy_json(r)
