# TigerDuck portal — observation + admin UI sitting alongside the backend.
#
# Multistage:
#   1. web-builder (node 20) — installs npm deps, runs Vite + tsc to
#      emit hashed bundles into web/dist/
#   2. runtime (python 3.13) — installs FastAPI + asyncpg, copies the
#      app/ source and the prebuilt web/dist/ from stage 1
#
# We DO NOT ship node into the runtime image. The portal serves the
# already-compiled bundle as static files; React Router does in-browser
# routing on top.
#
# Intentional difference from server/Dockerfile: `postgresql-client` is
# in the runtime image so we can shell out to pg_dump for the export
# feature without pulling a python-side pg-dump lib.
#
# We do NOT ship the docker CLI. The status page talks to the docker
# engine via its REST API over the mounted /var/run/docker.sock (UDS),
# which keeps the image ~150 MB smaller and side-steps the distro-
# specific docker.io / docker-ce packaging tangle.

# ---------------------------------------------------------------------------
# Stage 1: build the React bundle.
# ---------------------------------------------------------------------------
FROM node:20-alpine AS web-builder

WORKDIR /web

# Cache the npm install layer: only re-runs when package*.json changes.
COPY web/package.json web/package-lock.json* ./
RUN npm ci --no-audit --no-fund

# The full source comes in after deps so a source-only change doesn't
# bust the install layer.
COPY web/ ./
RUN npm run build

# ---------------------------------------------------------------------------
# Stage 2: python runtime.
# ---------------------------------------------------------------------------
FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# postgresql-client gives us pg_dump (export) + pg_restore (import).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        postgresql-client \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install python deps separately from app code so the layer is cacheable.
COPY pyproject.toml ./
RUN pip install --upgrade pip \
    && pip install \
        fastapi==0.115.* \
        "uvicorn[standard]==0.32.*" \
        python-multipart==0.0.* \
        httpx==0.28.* \
        asyncpg==0.30.* \
        pydantic-settings==2.7.*

COPY app ./app
# Built SPA bundle from stage 1. main.py looks for it at
# <portal-root>/web/dist relative to app/, which inside the container is
# /app/web/dist.
COPY --from=web-builder /web/dist ./web/dist

EXPOSE 40010

# Lifespan-driven config load runs on startup (see app.main:lifespan).
# No `entrypoint.sh` because there's nothing complex to set up.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "40010"]
