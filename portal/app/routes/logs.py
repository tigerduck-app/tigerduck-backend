"""Logs page — one tab per source, server-rendered.

The tab strip is implemented as plain links carrying `?source=…` so the
URL is bookmarkable and only the selected source's logs are fetched per
page load. Tail width and the search box live on each tab.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from ..logs import (
    ANDROID_NEEDLES,
    APPLE_NEEDLES,
    DEFAULT_TAIL,
    MAX_TAIL,
    container_logs,
    filter_lines,
)

router = APIRouter()

# Tab id → (label, kind, container, needles). `kind` is "raw" for direct
# container logs or "filter" for substring-filtered backend logs.
TABS: list[dict] = [
    {"id": "backend", "label": "Backend",  "kind": "raw",    "container": "tigerduck-internal", "needles": None},
    {"id": "db",      "label": "DB",       "kind": "raw",    "container": "tigerduck-db",       "needles": None},
    {"id": "portal",  "label": "Portal",   "kind": "raw",    "container": "tigerduck-portal",   "needles": None},
    {"id": "android", "label": "Android",  "kind": "filter", "container": "tigerduck-internal", "needles": ANDROID_NEEDLES},
    {"id": "apple",   "label": "Apple",    "kind": "filter", "container": "tigerduck-internal", "needles": APPLE_NEEDLES},
]
TABS_BY_ID = {t["id"]: t for t in TABS}


@router.get("/logs")
async def logs_page(request: Request, source: str = "backend", tail: int = DEFAULT_TAIL):
    tab = TABS_BY_ID.get(source) or TABS_BY_ID["backend"]
    tail = max(1, min(tail, MAX_TAIL))

    res = await container_logs(tab["container"], tail=tail)
    if res["ok"] and tab["kind"] == "filter":
        text = filter_lines(res["text"], tab["needles"])
    else:
        text = res["text"]

    return request.app.state.templates.TemplateResponse(
        request,
        "logs.html",
        {
            "tabs": TABS,
            "active": tab,
            "tail": tail,
            "max_tail": MAX_TAIL,
            "log_ok": res["ok"],
            "log_detail": res.get("detail"),
            "log_text": text,
        },
    )


@router.get("/logs/data", response_class=PlainTextResponse)
async def logs_data(source: str = "backend", tail: int = DEFAULT_TAIL):
    """Plain-text tail for the live-update poller in logs.html.

    Returns the same content the HTML page would render (raw or
    needle-filtered, depending on the tab). On engine error returns the
    detail string with status 200 so the JS poller just shows it inline
    rather than treating a transient docker socket hiccup as fatal.
    """
    tab = TABS_BY_ID.get(source) or TABS_BY_ID["backend"]
    tail = max(1, min(tail, MAX_TAIL))
    res = await container_logs(tab["container"], tail=tail)
    if not res["ok"]:
        return res.get("detail") or "log unavailable"
    if tab["kind"] == "filter":
        return filter_lines(res["text"], tab["needles"])
    return res["text"]
