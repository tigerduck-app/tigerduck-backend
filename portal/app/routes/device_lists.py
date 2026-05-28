"""Device-lists proxy.

Forwards `/api/device-lists/*` to the backend's `/v2/device-lists/*` over
the docker internal URL with `X-Push-Token` attached, so the SPA never
sees the shared secret. Same shape as `custom_push.py`.
"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from ..status import BACKEND_INTERNAL_URL

router = APIRouter(prefix="/api/device-lists")


async def _backend_request(
    request: Request,
    method: str,
    path: str,
    *,
    json: dict | None = None,
    params: dict | None = None,
    timeout_s: float = 15.0,
) -> httpx.Response:
    secret = request.app.state.settings.api_shared_secret
    headers: dict[str, str] = {}
    if secret:
        headers["X-Push-Token"] = secret
    url = f"{BACKEND_INTERNAL_URL}/v2{path}"
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        return await client.request(
            method, url, headers=headers, json=json, params=params
        )


def _proxy(r: httpx.Response) -> JSONResponse | Response:
    # 204 No Content has no body — forwarding as JSON would emit "null"
    # and confuse fetch() callers that check `response.ok` without
    # parsing.
    if r.status_code == 204:
        return Response(status_code=204)
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


@router.get("")
async def list_lists(request: Request):
    try:
        r = await _backend_request(request, "GET", "/device-lists")
    except httpx.HTTPError as exc:
        return _proxy_error(exc)
    return _proxy(r)


@router.post("")
async def create_list(request: Request):
    body = await request.json()
    try:
        r = await _backend_request(request, "POST", "/device-lists", json=body)
    except httpx.HTTPError as exc:
        return _proxy_error(exc)
    return _proxy(r)


@router.get("/{list_id}")
async def get_list(list_id: int, request: Request):
    try:
        r = await _backend_request(request, "GET", f"/device-lists/{list_id}")
    except httpx.HTTPError as exc:
        return _proxy_error(exc)
    return _proxy(r)


@router.patch("/{list_id}")
async def update_list(list_id: int, request: Request):
    body = await request.json()
    try:
        r = await _backend_request(
            request, "PATCH", f"/device-lists/{list_id}", json=body
        )
    except httpx.HTTPError as exc:
        return _proxy_error(exc)
    return _proxy(r)


@router.delete("/{list_id}")
async def delete_list(list_id: int, request: Request):
    try:
        r = await _backend_request(
            request, "DELETE", f"/device-lists/{list_id}"
        )
    except httpx.HTTPError as exc:
        return _proxy_error(exc)
    return _proxy(r)


@router.get("/{list_id}/members")
async def list_members(list_id: int, request: Request):
    limit = request.query_params.get("limit", "500")
    offset = request.query_params.get("offset", "0")
    try:
        r = await _backend_request(
            request,
            "GET",
            f"/device-lists/{list_id}/members",
            params={"limit": limit, "offset": offset},
        )
    except httpx.HTTPError as exc:
        return _proxy_error(exc)
    return _proxy(r)


@router.post("/{list_id}/members")
async def add_members(list_id: int, request: Request):
    body = await request.json()
    try:
        r = await _backend_request(
            request, "POST", f"/device-lists/{list_id}/members", json=body
        )
    except httpx.HTTPError as exc:
        return _proxy_error(exc)
    return _proxy(r)


@router.delete("/{list_id}/members/{device_id}")
async def remove_member(list_id: int, device_id: str, request: Request):
    try:
        r = await _backend_request(
            request, "DELETE", f"/device-lists/{list_id}/members/{device_id}"
        )
    except httpx.HTTPError as exc:
        return _proxy_error(exc)
    return _proxy(r)
