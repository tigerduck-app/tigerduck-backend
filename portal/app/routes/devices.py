"""Devices list proxy.

Forwards `/api/devices` to the backend's `/v2/devices` with `X-Push-Token`
attached, so the SPA never sees the shared secret. Same pattern as
custom_push.py — see that module's docstring for the rationale.
"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..status import BACKEND_INTERNAL_URL

router = APIRouter(prefix="/api/devices")


@router.get("")
async def list_devices(request: Request) -> JSONResponse:
    secret = request.app.state.settings.api_shared_secret
    headers: dict[str, str] = {}
    if secret:
        headers["X-Push-Token"] = secret
    limit = request.query_params.get("limit", "200")
    offset = request.query_params.get("offset", "0")
    url = f"{BACKEND_INTERNAL_URL}/v2/devices"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                url, headers=headers, params={"limit": limit, "offset": offset}
            )
    except httpx.HTTPError as exc:
        return JSONResponse(
            status_code=502,
            content={
                "detail": f"backend call failed: {type(exc).__name__}: {exc}"
            },
        )
    try:
        body = r.json() if r.content else {}
    except ValueError:
        body = {"detail": r.text or f"HTTP {r.status_code}"}
    return JSONResponse(status_code=r.status_code, content=body)
