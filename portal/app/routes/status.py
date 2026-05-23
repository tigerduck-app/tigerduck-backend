"""Status page — read-only home of the portal."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..status import (
    backend_version,
    docker_containers,
    file_presence,
    llm_health,
    postgres_health,
)

router = APIRouter()


@router.get("/health")
async def health() -> JSONResponse:
    """Liveness — no DB hits. Used by `docker compose` healthcheck
    if we ever wire one for the portal container."""
    return JSONResponse({"status": "ok"})


@router.get("/", response_class=HTMLResponse)
async def status_page(request: Request) -> HTMLResponse:
    settings = request.app.state.settings
    # Fan these out — they're independent and each has its own timeout,
    # so running serially makes the worst case (everything down) ≈ 11 s
    # of wall time. gather drops it to the longest single timeout (~3 s),
    # which matters exactly when an operator hits this page to diagnose
    # a hung stack.
    pg, llm, containers, version_info = await asyncio.gather(
        postgres_health(settings.database_url),
        llm_health(settings.llm_base_url),
        docker_containers(),
        backend_version(),
    )
    secrets = {
        "apns_key": file_presence(settings.apns_key_path),
        "fcm_credentials": file_presence(settings.fcm_credentials_path),
    }

    return request.app.state.templates.TemplateResponse(
        request,
        "status.html",
        {
            "env": settings.env,
            "apns_env": settings.apns_env,
            "log_level": settings.log_level,
            "skip_llm_probe": settings.skip_llm_probe,
            "llm_base_url": settings.llm_base_url,
            "backend_public_url": settings.backend_public_url,
            "portal_public_url": settings.portal_public_url,
            "host_lan_ips": settings.host_lan_ips,
            "containers": containers,
            "postgres": pg,
            "llm": llm,
            "secrets": secrets,
            "backend_version": version_info,
        },
    )
