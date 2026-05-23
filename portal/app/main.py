"""TigerDuck portal — FastAPI entrypoint.

Single-process uvicorn. SQLite for portal-local state, asyncpg for
reading the backend DB on the status page. No auth middleware — the
`require_admin` dependency on each route does the Cloudflare-header
check (see app/auth.py for why).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import Settings
from .db import init_db
from .routes import admins, backup, custom_push, logs, status


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings.from_env()
    init_db(settings.portal_db_path, settings.portal_bootstrap_admin)

    app_dir = Path(__file__).resolve().parent
    templates = Jinja2Templates(directory=str(app_dir / "templates"))

    app.state.settings = settings
    app.state.templates = templates
    yield


app = FastAPI(title="TigerDuck portal", lifespan=lifespan)

# Mount static FIRST so the route prefix doesn't shadow the file server.
_static_dir = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

app.include_router(status.router)
app.include_router(logs.router)
app.include_router(admins.router)
app.include_router(backup.router)
app.include_router(custom_push.router)
