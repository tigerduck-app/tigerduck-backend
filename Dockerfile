# syntax=docker/dockerfile:1.7

# -------- stage 1: resolve dependencies with uv ------------------------------
# `--mount=type=cache` lets uv reuse the wheel cache across builds, so
# `docker compose build` after a pyproject tweak only rebuilds the deps that
# actually changed.

FROM python:3.13-slim-bookworm AS builder

# Install uv from its official image. Pinning a version would be safer for
# reproducible builds; 'latest' is fine for development.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app
ENV UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_PYTHON_DOWNLOADS=never

# 1. Dependencies layer — cache-friendly: only busts when pyproject/uv.lock changes.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# 2. Source — busts whenever code changes but deps don't rebuild.
COPY server ./server
# String bundles for server-composed push copy. `server/i18n.py` reads them
# from `app-translation/generated/backend` beside `server/`, and the app
# refuses to start without the fallback one. They come from the
# app-translation submodule, so the checkout this is built from must have it
# (`git submodule update --init`); without it this COPY fails the build
# instead of shipping an image that cannot compose a single push.
COPY app-translation/generated/backend ./app-translation/generated/backend
COPY scripts ./scripts
COPY alembic.ini ./
COPY entrypoint.sh ./

# Final install wires the local package into the venv. --no-install-project
# above skipped this while the cache was cold, so do it now with sources present.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# -------- stage 2: runtime ---------------------------------------------------
FROM python:3.13-slim-bookworm

# trafilatura / lxml pull libxml2 + libxslt at runtime. slim-bookworm has them
# in /usr/lib already; no apt install needed. If a future dep requires native
# libs, add them here (NOT in the builder stage).

WORKDIR /app

# Copy the resolved venv and the source tree from builder.
COPY --from=builder /app /app

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UVICORN_PORT=40000

EXPOSE 40000
CMD ["./entrypoint.sh"]
