"""Links (`/links/<name>` on the API): list, create, edit, disable and
test now.

Every action goes to the backend's admin listener, which validates the
target, writes the change and its `admin_audit_log` row in one
transaction, and runs the checks. The portal names the operator in
`X-Admin-Actor`.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .moodle.push import forward_to_backend

router = APIRouter(prefix="/api/links")

# A check may take the backend two 35s fetches and a 3s pause
# (server/links/checker.py); Cloudflare gives up after 100s.
_TIMEOUT_SECONDS = 85
# The backend's link names; anything else could rewrite the forwarded path.
_NAME = re.compile(r"[a-z0-9-]{1,64}")


async def _forward(request: Request, method: str, path: str = "", json=None) -> JSONResponse:
    return await forward_to_backend(
        request, method, f"/admin/links{path}", json, timeout=_TIMEOUT_SECONDS
    )


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
