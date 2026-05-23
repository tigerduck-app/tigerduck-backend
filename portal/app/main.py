"""TigerDuck Backend Portal — FastAPI entrypoint.

Single-process uvicorn. Stateless: postgres is owned by the backend
container, status data is fetched on each request. No auth — front with
Cloudflare Zero Trust (or any other auth-proxy) if you need a gate.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import Settings
from .routes import backup, custom_push, logs, status, test_push


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings.from_env()

    app_dir = Path(__file__).resolve().parent
    templates = Jinja2Templates(directory=str(app_dir / "templates"))
    # Surfaced to every template so _base.html can hide dev-only nav
    # entries in prod without each route having to pass `env` through
    # its TemplateResponse context. Routes that need a different value
    # (per-request) can still override via the context dict.
    templates.env.globals["env"] = settings.env

    app.state.settings = settings
    app.state.templates = templates
    yield


app = FastAPI(title="TigerDuck Backend Portal", lifespan=lifespan)

# Mount static FIRST so the route prefix doesn't shadow the file server.
_static_dir = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

app.include_router(status.router)
app.include_router(logs.router)
app.include_router(backup.router)
app.include_router(custom_push.router)
app.include_router(test_push.router)
