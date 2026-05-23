"""Logs page — surfaces docker engine logs in topical sections."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from ..auth import require_admin
from ..logs import (
    ANDROID_NEEDLES,
    APPLE_NEEDLES,
    DEFAULT_TAIL,
    MAX_TAIL,
    container_logs,
    filter_lines,
)

router = APIRouter()

CONTAINERS = ["tigerduck-internal", "tigerduck-db", "tigerduck-portal"]


@router.get("/logs")
async def logs_page(
    request: Request,
    actor: Annotated[str, Depends(require_admin)],
    tail: int = DEFAULT_TAIL,
):
    tail = max(1, min(tail, MAX_TAIL))

    # Pull each container's logs once. Backend logs feed three sections
    # (raw + android filter + apple filter); db and portal feed only
    # their raw section.
    container_blocks: list[dict] = []
    backend_text = ""
    for name in CONTAINERS:
        res = await container_logs(name, tail=tail)
        container_blocks.append({"name": name, **res})
        if name == "tigerduck-internal" and res["ok"]:
            backend_text = res["text"]

    android_text = filter_lines(backend_text, ANDROID_NEEDLES)
    apple_text = filter_lines(backend_text, APPLE_NEEDLES)

    return request.app.state.templates.TemplateResponse(
        request,
        "logs.html",
        {
            "actor": actor,
            "tail": tail,
            "max_tail": MAX_TAIL,
            "containers": container_blocks,
            "android_text": android_text,
            "apple_text": apple_text,
            "has_backend": bool(backend_text),
        },
    )
