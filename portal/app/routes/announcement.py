"""Announcement composer.

Lets an operator author a new bulletin or patch an existing one. New
bulletins are inserted directly into the `bulletins` table with
`processing_state='processed'` so the dispatcher's regular tick fans
them out on its next pass — the same path LLM-classified bulletins
take. Patches update display fields only and don't re-fire the push.

Available in both dev and prod. In prod the page's JS pops a confirm
dialog before any mutation so a stray tab doesn't accidentally
broadcast to every registered device.
"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..status import BACKEND_INTERNAL_URL

router = APIRouter()


async def _backend_request(
    request: Request,
    method: str,
    path: str,
    *,
    json: dict | None = None,
    params: dict | None = None,
    timeout_s: float = 15.0,
) -> httpx.Response:
    """Call `tigerduck-internal:40000/v2<path>` over the docker network."""
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
    """Mirror upstream status + body so the page's JS gets the real error."""
    try:
        body = r.json() if r.content else {}
    except ValueError:
        body = {"detail": r.text or f"HTTP {r.status_code}"}
    return JSONResponse(status_code=r.status_code, content=body)


# --- Page render ----------------------------------------------------------


@router.get("/announcement", response_class=HTMLResponse)
async def page(request: Request) -> HTMLResponse:
    """Initial render — table is paged in via fetch() so a slow backend
    doesn't block the page shell. Taxonomy is fetched once and embedded
    so the org / tag dropdowns render without an extra round-trip."""
    taxonomy: dict | None = None
    taxonomy_error: str | None = None
    try:
        r = await _backend_request(request, "GET", "/bulletins/taxonomy")
        if r.status_code == 200:
            taxonomy = r.json()
        else:
            taxonomy_error = f"backend HTTP {r.status_code}: {r.text[:200]}"
    except httpx.HTTPError as exc:
        taxonomy_error = f"{type(exc).__name__}: {exc}"
    return request.app.state.templates.TemplateResponse(
        request,
        "announcement.html",
        {
            "taxonomy": taxonomy,
            "taxonomy_error": taxonomy_error,
        },
    )


# --- JSON proxies ---------------------------------------------------------


@router.get("/announcement/api/list", response_class=JSONResponse)
async def api_list(
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
        return JSONResponse(
            status_code=502,
            content={"detail": f"backend call failed: {type(exc).__name__}: {exc}"},
        )
    return _proxy_json(r)


@router.get("/announcement/api/{bulletin_id}", response_class=JSONResponse)
async def api_get(bulletin_id: int, request: Request) -> JSONResponse:
    try:
        r = await _backend_request(request, "GET", f"/bulletins/{bulletin_id}")
    except httpx.HTTPError as exc:
        return JSONResponse(
            status_code=502,
            content={"detail": f"backend call failed: {type(exc).__name__}: {exc}"},
        )
    return _proxy_json(r)


class _CreateSubmission(BaseModel):
    # Length limits mirror BulletinAdminCreateRequest so a malformed
    # payload fails fast on the portal side instead of paying a
    # cross-container hop. Extra fields are rejected so a typo'd key
    # in the UI doesn't get silently dropped on the wire.
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


@router.post("/announcement/api/create", response_class=JSONResponse)
async def api_create(
    payload: _CreateSubmission, request: Request
) -> JSONResponse:
    try:
        r = await _backend_request(
            request, "POST", "/bulletins/admin", json=payload.model_dump()
        )
    except httpx.HTTPError as exc:
        return JSONResponse(
            status_code=502,
            content={"detail": f"backend call failed: {type(exc).__name__}: {exc}"},
        )
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


@router.patch("/announcement/api/{bulletin_id}", response_class=JSONResponse)
async def api_update(
    bulletin_id: int, payload: _UpdateSubmission, request: Request
) -> JSONResponse:
    if bulletin_id <= 0:
        raise HTTPException(status_code=422, detail="bulletin_id must be positive")
    # exclude_unset so the backend's PATCH sees only the fields the
    # operator actually changed — omitted keys leave columns untouched.
    body = payload.model_dump(exclude_unset=True)
    try:
        r = await _backend_request(
            request, "PATCH", f"/bulletins/admin/{bulletin_id}", json=body
        )
    except httpx.HTTPError as exc:
        return JSONResponse(
            status_code=502,
            content={"detail": f"backend call failed: {type(exc).__name__}: {exc}"},
        )
    return _proxy_json(r)
