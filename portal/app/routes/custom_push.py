"""Placeholder for the future custom-push UI. Renders a "coming soon"
page so the nav slot exists; scope sits in docs/portal-design.md."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/custom-push", response_class=HTMLResponse)
async def page(request: Request) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request, "custom_push.html", {}
    )
