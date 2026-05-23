"""Placeholder for the future custom-push UI. Just renders a "coming
soon" page so the nav slot exists; the design doc spells out scope."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ..auth import require_admin

router = APIRouter()


@router.get("/custom-push", response_class=HTMLResponse)
async def page(
    request: Request,
    actor: Annotated[str, Depends(require_admin)],
) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request, "custom_push.html", {"actor": actor}
    )
