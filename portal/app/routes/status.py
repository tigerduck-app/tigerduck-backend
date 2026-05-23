"""Status page — read-only home of the portal."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..auth import current_user_email, get_settings, require_admin
from ..config import Settings
from ..status import (
    docker_containers,
    file_presence,
    llm_health,
    postgres_health,
)

router = APIRouter()


@router.get("/health")
async def health() -> JSONResponse:
    """Liveness — no admin gate, no DB hits. Used by `docker compose`
    healthcheck if we ever wire one for the portal container."""
    return JSONResponse({"status": "ok"})


@router.get("/", response_class=HTMLResponse)
async def status_page(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    actor: Annotated[str, Depends(require_admin)],
) -> HTMLResponse:
    pg = await postgres_health(settings.database_url)
    llm = await llm_health(settings.llm_base_url)
    containers = await docker_containers()
    secrets = {
        "apns_key": file_presence(settings.apns_key_path),
        "fcm_credentials": file_presence(settings.fcm_credentials_path),
    }

    return request.app.state.templates.TemplateResponse(
        request,
        "status.html",
        {
            "actor": actor,
            "current_user": current_user_email(request, settings),
            "env": settings.env,
            "apns_env": settings.apns_env,
            "log_level": settings.log_level,
            "skip_llm_probe": settings.skip_llm_probe,
            "llm_base_url": settings.llm_base_url,
            "containers": containers,
            "postgres": pg,
            "llm": llm,
            "secrets": secrets,
        },
    )
