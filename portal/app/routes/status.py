"""Status JSON endpoints.

`/api/env` is split out from `/api/status` so the React layout can render
the nav and env badge without paying for the (slow) docker/postgres
health checks every time the SPA mounts.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..status import (
    backend_version,
    docker_containers,
    file_presence,
    llm_health,
    postgres_health,
)

router = APIRouter(prefix="/api")


@router.get("/env")
async def env_info(request: Request) -> JSONResponse:
    """Cheap config-only payload the layout needs on every page load."""
    s = request.app.state.settings
    return JSONResponse(
        {
            "env": s.env,
            "apns_env": s.apns_env,
            "log_level": s.log_level,
            "llm_base_url": s.llm_base_url,
            "skip_llm_probe": s.skip_llm_probe,
            "backend_public_url": s.backend_public_url,
            "portal_public_url": s.portal_public_url,
            "host_lan_ips": s.host_lan_ips,
        }
    )


@router.get("/status")
async def status_payload(request: Request) -> JSONResponse:
    settings = request.app.state.settings
    # Fan checks out — independent + each has its own timeout, so the
    # serial worst-case (everything down) is ~11 s. gather drops it to
    # the longest single timeout (~3 s), which matters precisely when an
    # operator hits the status page to diagnose a hung stack.
    pg, llm, containers, version_info = await asyncio.gather(
        postgres_health(settings.database_url),
        llm_health(settings.llm_base_url),
        docker_containers(),
        backend_version(),
    )
    return JSONResponse(
        {
            "env": {
                "env": settings.env,
                "apns_env": settings.apns_env,
                "log_level": settings.log_level,
                "llm_base_url": settings.llm_base_url,
                "skip_llm_probe": settings.skip_llm_probe,
                "backend_public_url": settings.backend_public_url,
                "portal_public_url": settings.portal_public_url,
                "host_lan_ips": settings.host_lan_ips,
            },
            "containers": containers,
            "postgres": pg,
            "llm": llm,
            "backend_version": version_info,
            "secrets": {
                "apns_key": file_presence(settings.apns_key_path),
                "fcm_credentials": file_presence(settings.fcm_credentials_path),
            },
        }
    )


# `/health` stays out of /api so external healthchecks (compose,
# Cloudflare, k8s) keep working without knowing about the SPA's url
# scheme. Liveness only — no DB hits.
liveness_router = APIRouter()


@liveness_router.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})
