"""Links (`/links/<name>` on the API): list, create, edit, disable and
test now.

Every action goes to the backend's admin listener, which validates the
target, writes the change and its `admin_audit_log` row in one
transaction, and runs the checks. The portal names the operator in
`X-Admin-Actor`.
"""
from __future__ import annotations

import re

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .moodle._shared import logger
from .moodle.push import BACKEND_ADMIN_URL, _internal_headers

router = APIRouter(prefix="/api/links")

# A check may take the backend two 35s fetches and a 3s pause
# (server/links/checker.py); Cloudflare gives up after 100s.
_TIMEOUT_SECONDS = 85
# The backend's link names; anything else could rewrite the forwarded path.
_NAME = re.compile(r"[a-z0-9-]{1,64}")


async def _forward(request: Request, method: str, path: str = "", json=None) -> JSONResponse:
    headers = {**_internal_headers(request), "X-Admin-Actor": request.state.actor}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.request(
                method, f"{BACKEND_ADMIN_URL}/admin/links{path}", headers=headers, json=json
            )
    except httpx.HTTPError as exc:
        logger.exception("links call to the backend failed")
        return JSONResponse({"detail": f"backend unreachable: {exc}"}, status_code=502)
    try:
        payload = resp.json()
    except ValueError:
        payload = {"detail": f"HTTP {resp.status_code}: {resp.text[:300]}"}
    return JSONResponse(payload, status_code=resp.status_code)


def _unknown() -> JSONResponse:
    return JSONResponse({"detail": "link not found"}, status_code=404)


async def _body(request: Request):
    """The request's JSON, or None for the backend to refuse as invalid."""
    try:
        return await request.json()
    except ValueError:
        return None


@router.get("")
async def list_links(request: Request) -> JSONResponse:
    return await _forward(request, "GET")


@router.post("")
async def create_link(request: Request) -> JSONResponse:
    return await _forward(request, "POST", json=await _body(request))


@router.patch("/{name}")
async def update_link(name: str, request: Request) -> JSONResponse:
    if not _NAME.fullmatch(name):
        return _unknown()
    return await _forward(request, "PATCH", f"/{name}", json=await _body(request))


@router.post("/{name}/test")
async def test_link(name: str, request: Request) -> JSONResponse:
    if not _NAME.fullmatch(name):
        return _unknown()
    return await _forward(request, "POST", f"/{name}/test")
