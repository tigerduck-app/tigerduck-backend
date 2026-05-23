# TigerDuck portal — observation + admin UI sitting alongside the backend.
#
# Intentional difference from server/Dockerfile: `postgresql-client` is in
# the image so we can shell out to pg_dump for the export feature without
# pulling a python-side pg-dump lib.
#
# We do NOT ship the docker CLI. The status page talks to the docker
# engine via its REST API over the mounted /var/run/docker.sock (UDS),
# which keeps the image ~150 MB smaller and side-steps the distro-
# specific docker.io / docker-ce packaging tangle.

FROM python:3.13-slim

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

# Install deps separately from app code so the layer is cacheable.
COPY pyproject.toml ./
RUN pip install --upgrade pip \
    && pip install \
        fastapi==0.115.* \
        "uvicorn[standard]==0.32.*" \
        jinja2==3.1.* \
        python-multipart==0.0.* \
        httpx==0.28.* \
        asyncpg==0.30.* \
        pydantic-settings==2.7.*

COPY app ./app

# Created by the docker volume mount; this just documents the path so
# the SQLite DB path resolves consistently in dev runs too.
VOLUME ["/data"]

EXPOSE 40010

# Lifespan-driven schema migration runs on startup (see app.db).
# No `entrypoint.sh` because there's nothing complex to set up.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "40010"]
