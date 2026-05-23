"""Logs JSON endpoints.

The React side renders the tab strip from `/api/logs/tabs` and tails a
specific source via `/api/logs/data?source=&tail=`. Same back-end data
funnel as before; only the serialization changed.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..logs import (
    ANDROID_NEEDLES,
    APPLE_NEEDLES,
    DEFAULT_TAIL,
    MAX_TAIL,
    container_logs,
    filter_lines,
)

router = APIRouter(prefix="/api/logs")

TABS: list[dict] = [
    {"id": "backend", "label": "Backend",  "kind": "raw",    "container": "tigerduck-internal", "needles": None},
    {"id": "db",      "label": "DB",       "kind": "raw",    "container": "tigerduck-db",       "needles": None},
    {"id": "portal",  "label": "Portal",   "kind": "raw",    "container": "tigerduck-portal",   "needles": None},
    {"id": "android", "label": "Android",  "kind": "filter", "container": "tigerduck-internal", "needles": ANDROID_NEEDLES},
    {"id": "apple",   "label": "Apple",    "kind": "filter", "container": "tigerduck-internal", "needles": APPLE_NEEDLES},
]
TABS_BY_ID = {t["id"]: t for t in TABS}


@router.get("/tabs")
async def tabs() -> JSONResponse:
    """Static metadata the SPA renders the tab strip from. Includes the
    needle lists so the UI can show which lines a filter tab matches."""
    return JSONResponse({"tabs": TABS, "default_tail": DEFAULT_TAIL, "max_tail": MAX_TAIL})


@router.get("/data")
async def data(source: str = "backend", tail: int = DEFAULT_TAIL) -> JSONResponse:
    """One-shot tail. `ok=false` returns 200 with `detail` so a transient
    docker socket hiccup shows inline rather than killing the poller."""
    tab = TABS_BY_ID.get(source) or TABS_BY_ID["backend"]
    tail = max(1, min(tail, MAX_TAIL))
    res = await container_logs(tab["container"], tail=tail)
    if not res["ok"]:
        return JSONResponse(
            {"ok": False, "text": "", "detail": res.get("detail") or "log unavailable"}
        )
    text = (
        filter_lines(res["text"], tab["needles"])
        if tab["kind"] == "filter"
        else res["text"]
    )
    return JSONResponse({"ok": True, "text": text})
