"""Announcement composer proxies (JSON-only).

The portal forwards reads + writes to the backend's
`/v2/bulletins` + `/v2/bulletins/admin` endpoints over the docker
network. Writes go through `admin/*` which is gated by the
`X-Push-Token` shared secret on the backend; the portal attaches that
header here so the SPA never sees the secret.
"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..status import BACKEND_INTERNAL_URL

router = APIRouter(prefix="/api/announcement")


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


@router.get("/taxonomy")
async def taxonomy(request: Request) -> JSONResponse:
    try:
        r = await _backend_request(request, "GET", "/bulletins/taxonomy")
    except httpx.HTTPError as exc:
        return _proxy_error(exc)
    return _proxy_json(r)


@router.get("/list")
async def list_bulletins(
    request: Request,
    limit: int = 30,
    cursor: int | None = None,
    include_deleted: bool = False,
) -> JSONResponse:
    params: dict = {"limit": limit, "include_deleted": str(include_deleted).lower()}
    if cursor is not None:
        params["cursor"] = cursor
    try:
        r = await _backend_request(request, "GET", "/bulletins", params=params)
    except httpx.HTTPError as exc:
        return _proxy_error(exc)
    return _proxy_json(r)


@router.get("/{bulletin_id}")
async def get_bulletin(bulletin_id: int, request: Request) -> JSONResponse:
    try:
        r = await _backend_request(request, "GET", f"/bulletins/{bulletin_id}")
    except httpx.HTTPError as exc:
        return _proxy_error(exc)
    return _proxy_json(r)


class _CreateSubmission(BaseModel):
    # Length limits mirror BulletinAdminCreateRequest so a malformed
    # payload 422s before paying a cross-container hop. extra=forbid so
    # a typo'd UI key doesn't get silently dropped on the wire.
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=500)
    title_clean: str | None = Field(default=None, max_length=200)
    summary: str | None = Field(default=None, max_length=2000)
    body_clean: str | None = Field(default=None, max_length=20000)
    body_md: str | None = Field(default=None, max_length=20000)
    canonical_org: str = Field(min_length=1, max_length=32)
    content_tags: list[str] = Field(default_factory=list, max_length=8)
    importance: str = Field(default="normal", max_length=16)
    source_url: str = Field(
        default="https://announce.ntust.edu.tw/manual",
        min_length=1,
        max_length=1000,
    )


@router.post("")
async def create_bulletin(
    payload: _CreateSubmission, request: Request
) -> JSONResponse:
    try:
        r = await _backend_request(
            request, "POST", "/bulletins/admin", json=payload.model_dump()
        )
    except httpx.HTTPError as exc:
        return _proxy_error(exc)
    return _proxy_json(r)


class _UpdateSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=500)
    title_clean: str | None = Field(default=None, max_length=200)
    summary: str | None = Field(default=None, max_length=2000)
    body_clean: str | None = Field(default=None, max_length=20000)
    body_md: str | None = Field(default=None, max_length=20000)
    canonical_org: str | None = Field(default=None, max_length=32)
    content_tags: list[str] | None = Field(default=None, max_length=8)
    importance: str | None = Field(default=None, max_length=16)


@router.patch("/{bulletin_id}")
async def update_bulletin(
    bulletin_id: int, payload: _UpdateSubmission, request: Request
) -> JSONResponse:
    if bulletin_id <= 0:
        raise HTTPException(status_code=422, detail="bulletin_id must be positive")
    # exclude_unset → backend's PATCH only sees keys the operator
    # actually changed; omitted columns are left alone.
    body = payload.model_dump(exclude_unset=True)
    try:
        r = await _backend_request(
            request, "PATCH", f"/bulletins/admin/{bulletin_id}", json=body
        )
    except httpx.HTTPError as exc:
        return _proxy_error(exc)
    return _proxy_json(r)
