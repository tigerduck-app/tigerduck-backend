"""TLS pins: the sets, the watcher's last result per host and the document
history, read from tigerduck-db.

The actions go to the backend's admin listener, which alone holds the
signing key; it writes each action's audit row, so these proxies do not.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse

from ..db import get_pool
from .moodle._shared import logger
from .moodle.push import BACKEND_ADMIN_URL, _internal_headers

router = APIRouter(prefix="/api/tls-pins")

JsonBody = Annotated[dict, Body()]

# A pin no monitored host has served for this long is listed for removal.
STALE_AFTER = timedelta(days=90)

_SETS_SQL = """
SELECT id, domains, pins, expires_at, monitored_hosts, auto_publish,
       watch_results, updated_at, updated_by
FROM tls_pin_sets ORDER BY id
"""

_DOCUMENTS_SQL = """
SELECT serial, key_id, issued_at, expires_at, published_at, reason
FROM tls_pin_documents ORDER BY serial DESC LIMIT 50
"""


def _pin(pin: dict, now: datetime) -> dict:
    seen = pin.get("last_seen_at")
    stale = seen is not None and datetime.fromisoformat(seen) < now - STALE_AFTER
    return {**pin, "stale": stale}


@router.get("")
async def get_tls_pins(pool: Annotated[Any, Depends(get_pool)]) -> JSONResponse:
    now = datetime.now(UTC)
    async with pool.acquire() as conn:
        sets = await conn.fetch(_SETS_SQL)
        documents = await conn.fetch(_DOCUMENTS_SQL)
    return JSONResponse(
        {
            "sets": [
                {
                    "id": r["id"],
                    "domains": json.loads(r["domains"]),
                    "pins": [_pin(p, now) for p in json.loads(r["pins"])],
                    "expires_at": r["expires_at"].isoformat(),
                    "monitored_hosts": json.loads(r["monitored_hosts"]),
                    "auto_publish": r["auto_publish"],
                    "watch_results": json.loads(r["watch_results"]),
                    "updated_at": r["updated_at"].isoformat(),
                    "updated_by": r["updated_by"],
                }
                for r in sets
            ],
            "documents": [
                {
                    "serial": r["serial"],
                    "key_id": r["key_id"],
                    "issued_at": r["issued_at"].isoformat(),
                    "expires_at": r["expires_at"].isoformat(),
                    "published_at": r["published_at"].isoformat(),
                    "reason": r["reason"],
                }
                for r in documents
            ],
        },
        headers={"Cache-Control": "private, no-store"},
    )


async def _forward(request: Request, method: str, path: str, body: Any = None) -> JSONResponse:
    headers = {**_internal_headers(request), "X-Portal-Actor": request.state.actor}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(
                method, f"{BACKEND_ADMIN_URL}/tls-pins{path}", headers=headers, json=body
            )
    except httpx.HTTPError as exc:
        logger.exception("tls pin action %s failed", path)
        return JSONResponse({"detail": f"backend unreachable: {type(exc).__name__}"}, 502)
    try:
        payload = resp.json()
    except ValueError:
        payload = {"detail": f"HTTP {resp.status_code}: {resp.text[:300]}"}
    # A validation error's detail is a list; the SPA shows a string.
    if isinstance(payload, dict) and isinstance(payload.get("detail"), list):
        payload["detail"] = "; ".join(str(e.get("msg", e)) for e in payload["detail"])
    return JSONResponse(payload, status_code=resp.status_code)


def _set_path(set_id: str, rest: str) -> str:
    return f"/sets/{quote(set_id, safe='')}{rest}"


@router.post("/sets/{set_id}/pins")
async def add_pin(set_id: str, request: Request, body: JsonBody) -> JSONResponse:
    return await _forward(request, "POST", _set_path(set_id, "/pins"), body)


@router.post("/sets/{set_id}/pins/remove")
async def remove_pin(set_id: str, request: Request, body: JsonBody) -> JSONResponse:
    return await _forward(request, "POST", _set_path(set_id, "/pins/remove"), body)


@router.put("/sets/{set_id}/monitored-hosts")
async def set_monitored_hosts(
    set_id: str, request: Request, body: JsonBody
) -> JSONResponse:
    return await _forward(request, "PUT", _set_path(set_id, "/monitored-hosts"), body)


@router.put("/sets/{set_id}/expires-at")
async def extend_expiry(set_id: str, request: Request, body: JsonBody) -> JSONResponse:
    return await _forward(request, "PUT", _set_path(set_id, "/expires-at"), body)


@router.put("/sets/{set_id}/auto-publish")
async def set_auto_publish(
    set_id: str, request: Request, body: JsonBody
) -> JSONResponse:
    return await _forward(request, "PUT", _set_path(set_id, "/auto-publish"), body)


@router.post("/publish")
async def publish(request: Request, body: JsonBody) -> JSONResponse:
    return await _forward(request, "POST", "/publish", body)


@router.post("/resign")
async def resign(request: Request) -> JSONResponse:
    return await _forward(request, "POST", "/resign")
