"""TigerDuck Backend Portal — FastAPI entrypoint.

Serves a React SPA (built by `portal/web` and copied to `web/dist`)
plus a JSON API under `/api/*` that the SPA consumes. Stateless:
postgres is owned by the backend container, status data is fetched on
each request. No auth — front with Cloudflare Zero Trust (or any
other auth-proxy) if you need a gate.

Routing order matters:
  1. `/static/*` — favicon + logo (legacy assets the layout still uses)
  2. `/assets/*` — Vite-built JS/CSS hashed bundles
  3. `/api/*`  — JSON endpoints
  4. `/health` — liveness (outside /api so external probes don't need
                 to know about the SPA's URL scheme)
  5. anything else → `index.html` so the React Router can handle it
                     (client-side routes like /logs, /backup, etc.)
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .routes import announcement, backup, logs, status, test_push


_APP_DIR = Path(__file__).resolve().parent
_STATIC_DIR = _APP_DIR / "static"
# web/ lives one level up from app/ in the source tree. Inside the
# container both end up under /app per the Dockerfile COPY layout.
_WEB_DIST = _APP_DIR.parent / "web" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.settings = Settings.from_env()
    yield


app = FastAPI(title="TigerDuck Backend Portal", lifespan=lifespan)

# Legacy static assets (favicon, logo) — kept at /static so deep links
# from earlier portal versions (e.g. bookmarked icons) still resolve.
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

# Vite emits hashed bundles into web/dist/assets/. Only mount it if
# the build artifact exists so `npm run dev` (which serves assets via
# its own dev server at :5173) doesn't crash the portal on startup.
if (_WEB_DIST / "assets").exists():
    app.mount(
        "/assets",
        StaticFiles(directory=str(_WEB_DIST / "assets")),
        name="assets",
    )

app.include_router(status.router)
app.include_router(status.liveness_router)
app.include_router(logs.router)
app.include_router(backup.router)
app.include_router(announcement.router)
app.include_router(test_push.router)


@app.get("/{full_path:path}", include_in_schema=False)
async def spa(full_path: str, request: Request):
    """SPA catch-all.

    Falls through every other route (mounts and APIRouters are matched
    first, FastAPI evaluates routes in registration order). Anything
    that reaches here is treated as a client-side route — we return
    the same index.html and let React Router pick the view.

    If the bundle hasn't been built yet, return a clear instruction
    instead of a 500 so a fresh checkout knows what to do.
    """
    # Defensive: in case a path slips past the /api router (typo, version
    # skew between SPA and backend), 404 instead of returning the SPA
    # shell as JSON — the SPA would just render a "Page not found" view.
    if full_path.startswith("api/"):
        return JSONResponse({"detail": "not found"}, status_code=404)

    index = _WEB_DIST / "index.html"
    if not index.exists():
        return JSONResponse(
            {
                "detail": (
                    "Portal SPA bundle not found. Run `npm run build` in "
                    "portal/web/ (the Dockerfile does this automatically)."
                )
            },
            status_code=503,
        )
    return FileResponse(index, media_type="text/html")
