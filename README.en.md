<div align="center">
<a href="https://tigerduck.app/">
  <img width="2000" src="https://github.com/user-attachments/assets/cf6a1d18-a348-4b83-adfd-81c6dc82855f" alt="TigerDuck Backend Banner"/>
</a>
<br>

[![License](https://img.shields.io/github/license/tigerduck-app/tigerduck-backend?style=for-the-badge)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.13-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Postgres](https://img.shields.io/badge/Postgres-17-4169E1?style=for-the-badge&logo=postgresql&logoColor=white)](https://www.postgresql.org)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?style=for-the-badge&logo=docker&logoColor=white)](https://docs.docker.com/compose/)

[繁體中文](README.md) | **English**

</div>

## Overview

TigerDuck Backend is the server side of the [TigerDuck](https://github.com/tigerduck-app/tigerduck-app) iOS app. It runs at `api.tigerduck.app` and is responsible for three things:

- 📣 **Bulletin pipeline** — Scrape NTUST departmental announcements → de-duplicate → LLM classification (canonical_org / content_tags / importance) → match subscriptions → push
- 📲 **Push delivery** — APNs Push-to-Start (iOS Live Activity boot), FCM fan-out (Android), bad-token classification and cleanup
- ⏰ **Scheduling** — Live Activity token retry, class-table schedule sync, retention cleanup; all driven by a single APScheduler worker running inside the FastAPI lifespan

The service is deliberately **containerised, restart-safe, and stateless**: every bit of state lives in Postgres, so restarting the backend container loses no events and the scheduler simply resumes.

## Modules

### 📣 Bulletins (`server/bulletins/`)
- **scraper** — Fetches HTML from NTUST bulletin index pages and extracts metadata. NTUST's TLS chain is broken, so we ship a pinned CA bundle (falling back to `verify=False`).
- **dedup** — `content_hash` deduplication (same source + same hash is treated as a repost and marked `skipped`, no re-notification).
- **LLM classification** — OpenAI-compatible client (defaults to a host-side [llama-server](https://github.com/ggml-org/llama.cpp)). Returns `canonical_org` / `content_tags` / `importance` / `title_clean` / `summary` / `body_clean`.
- **Subscription matching + dispatch** — Matches each device's `BulletinSubscription` rules; on hit, sends APNs / FCM.
- **State machine** — `pending` → `processed` / `skipped` / `failed`. `failed` rows return to `pending` for another tick as long as attempts < max.

### 📲 Push
- **APNs** — JWT auth, Push-to-Start, Live Activity update / end
- **FCM** — Batched fan-out, automatic cleanup on `UNREGISTERED` / `SENDER_ID_MISMATCH`
- **Shared secret** — Mutating routes (device registration, subscription writes) require `X-Shared-Secret`; read routes (bulletin list / detail / taxonomy) are public

### ⏰ Scheduler
- **Single worker** — APScheduler runs inside the FastAPI lifespan; replica count is locked at 1. Multiple replicas would double-send (see [`docs/scheduler.md`](docs/scheduler.md)).
- **Tick design** — scrape / process / dispatch / retention each have their own interval trigger and don't block each other.

## Stack

| Layer | Choice |
|---|---|
| Web | FastAPI 0.115 + Uvicorn + structlog (JSON logs) |
| ORM | SQLAlchemy 2.x async + Alembic |
| DB | Postgres 17 (containerised, internal-only network) |
| Scheduling | APScheduler 3.x (IntervalTrigger) |
| Push | `aioapns` (APNs), `google-auth` + `httpx` (FCM v1) |
| LLM | OpenAI-compatible client → llama-server (host), `response_format: json_object` + JSON schema |
| Deployment | Docker Compose + nginx-proxy-manager |

## Architecture

```
       Public                                          host (macOS / Linux)
   ┌──────────────┐                              ┌────────────────────────────┐
   │  iOS / And.  │ ── HTTPS ──▶ nginx-proxy ──▶ │  tigerduck-internal        │
   └──────────────┘              -manager  ──┐   │  (FastAPI + APScheduler)   │
                                             │   │           │                │
   ┌──────────────┐                          │   │           ├── APNs        │
   │ Operator     │ ── HTTPS ──▶ cloudflared ─┼──▶│  tigerduck-portal         │
   └──────────────┘   (Zero Trust)           │   │  (FastAPI + Jinja, :40010)│
                                             │   │           │                │
                                             │   │           ▼                │
                                             │   │  ┌────────────────┐        │
                                             │   │  │ tigerduck-db   │        │
                                             │   │  │ (Postgres 17)  │        │
                                             │   │  └────────────────┘        │
                                             │   │           ▲                │
                                             │   │           │                │
                                             │   │  ┌────────────────┐        │
                                             │   │  │ llama-server   │◀───────┘
                                             │   │  │ (native, Metal)│
                                             │   │  └────────────────┘
                                             │   └────────────────────────────┘
                                             │
                                             └── proxy-net carries both backend and portal
```

- **`tigerduck-db` network**: internal-only bridge — Postgres has no route to the public internet.
- **`proxy-net`**: shared with nginx-proxy-manager; both backend and portal join it.
- **`tigerduck-host` (dev only)**: bridge added by `docker-compose.dev.yml` so backend `:40000` and portal `:40010` can publish to host ports.
- **llama-server**: runs natively on the host (Docker Desktop / macOS can't pass through Metal GPU). The backend reaches it via `host.docker.internal`.
- **portal**: stateless read-only operator UI. Ships without an app-level auth gate — front it with Cloudflare Zero Trust Application (or any auth-proxy) if you need one.

## Deployment

### Prerequisites

| Item | Requirement |
|---|---|
| OS | macOS / Linux (anything that runs Docker Compose) |
| Docker | Docker Engine 24+ / Docker Desktop 4.30+ |
| Postgres | 17 (brought up by compose; no host install needed) |
| llama-server | One machine that can serve a small instruct model (≤7B recommended) over an OpenAI-compatible endpoint |
| Reverse proxy | nginx-proxy-manager or equivalent, routing `api.<your-domain>` to `tigerduck-internal:40000` |

### Quick start

```bash
git clone https://github.com/tigerduck-app/tigerduck-backend.git
cd tigerduck-backend

# 1. Copy the template. Defaults to development mode (which auto-loads
#    docker-compose.dev.yml); for production deploys, flip TIGERDUCK_ENV.
cp .env.example .env

# 2. Drop the APNs key at server/secrets/AuthKey_<KEY_ID>.p8 (already gitignored)

# 3. Boot the stack (postgres + backend)
./start.sh                       # docker compose up -d --build + tail log

# 4. Health check
docker compose exec backend curl -sS localhost:40000/health
```

### Operator scripts

All four scripts read `TIGERDUCK_ENV` from `.env`; when it's `development` they additionally load `docker-compose.dev.yml` (publishes backend `:40000` + portal `:40010` to the host via a non-internal bridge network — the prod-only `proxy-net` is dropped because there's no NPM locally). The mode lives in `.env`; the scripts pick the right compose files automatically.

| Script | Purpose |
|---|---|
| `./start.sh` | `docker compose up -d --build`, then prints a status block (mode, ports, skip-LLM, …) |
| `./stop.sh` | `docker compose down` (volume preserved) |
| `./logs.sh` | Tail a service (defaults to `backend`) |
| `./clean-db.sh` | **Destructive** — drops the postgres volume; full reset (does NOT touch the portal's `tigerduck_portal_data` volume) |

### Operator portal

`tigerduck-portal` is a sibling compose service that comes up alongside the backend. Dev mode publishes it at `http://localhost:40010`; production typically lives behind cloudflared / Cloudflare Zero Trust if you want a signin gate (the portal itself does not enforce one). It can:

- Show stack status (every field `./start.sh` prints, plus containers via the docker engine UDS, backend version via `/version`, postgres row counts, LLM reachability, APNs/FCM secret presence, host LAN IPs as clickable links)
- Stream the last N lines of each container's logs with per-tab search; Android / Apple tabs are substring-filtered slices of the backend log
- Export `tigerduck-export-<timestamp>.tar.gz` (custom-format `pg_dump` + portal's SQLite + manifest); import the same format OR a bare `pg_dump` from a pre-portal install
- Custom-push placeholder (TODO, ships as a stub)

Full design: [`docs/portal-design.md`](docs/portal-design.md).

### LLM (host side)

The backend talks to a [llama-server](https://github.com/ggml-org/llama.cpp) running natively on the host:

```bash
# Example (a gemma-style instruct small model)
llama-server \
  --hf ggml-org/gemma-4-E4B-it-GGUF \
  --alias gemma-4-E4B-it-GGUF \
  --host 0.0.0.0 --port 40006 \
  --api-key <your-key> \
  --json-schema '{}'
```

Matching `.env`:

```dotenv
TIGERDUCK_LLM_BASE_URL=http://host.docker.internal:40006/v1
TIGERDUCK_LLM_API_KEY=<your-key>
TIGERDUCK_LLM_MODEL=gemma-4-E4B-it-GGUF
```

> ⚠️ Models that emit reasoning channels (harmony format, e.g. `<|channel>thought<channel|>`) are **incompatible** — the JSON parser only strips markdown fences, not channel markers. Pick a plain instruct model.

On macOS, `deploy/launchd/ai.tigerduck.llm.plist` wraps llama-server as a launchd service for long-running deployments.

## API Endpoints (v2)

| Method | Path | Purpose | Auth |
|---|---|---|---|
| `GET` | `/v2/health` | liveness | none |
| `POST` | `/v2/devices` | Device registration (APNs token, `platform=apple` / `android`) | shared secret |
| `GET` | `/v2/bulletins` | Bulletin list (cursor pagination, newest first) | none |
| `GET` | `/v2/bulletins/{id}` | Bulletin detail | none |
| `GET` | `/v2/bulletins/taxonomy` | org / tag label mapping | none |
| `GET/PUT` | `/v2/devices/{id}/subscriptions` | Subscription rules read/write | shared secret |
| `POST` | `/v2/live-activities/start-tokens` | Live Activity push-to-start token upload | shared secret |
| `POST` | `/v2/schedule/sync` | Class-table sync (feeds the Live Activity scheduler) | shared secret |

`/v1/*` is kept as a deprecated alias; iOS clients ≥ 1.6.1 use `/v2`.

## Development

```bash
# Host-side unit tests (no docker required)
uv sync
uv run pytest

# Alembic migration
uv run alembic revision --autogenerate -m "your change"
uv run alembic upgrade head
```

Production migrations are applied automatically by the container entrypoint (`entrypoint.sh`); no manual step is needed in normal operation.

## Project Structure

```
tigerduck-backend/
├── server/
│   ├── main.py                  # FastAPI entrypoint + lifespan (builds scheduler / LLM / push router)
│   ├── config.py                # pydantic-settings; every setting reads from TIGERDUCK_* env
│   ├── db.py / models.py        # SQLAlchemy async engine, DeviceRegistration
│   ├── security.py              # shared-secret dependency
│   ├── _ssl_compat.py           # Lenient OpenSSL 3 mode (NTUST's TLS chain is broken)
│   ├── routes/                  # devices / schedule / bulletins / live_activities / debug
│   ├── push/                    # apns_client / fcm_client / payload / router
│   ├── scheduler/               # APScheduler runtime, dispatch, retention
│   ├── bulletins/               # scraper / dedup / matcher / dispatcher / taxonomy
│   │   └── llm/                 # OpenAI-compatible client + prompt
│   ├── secrets/                 # APNs .p8 (gitignored)
│   ├── migrations/              # Alembic
│   └── tests/                   # pytest (unit + integration)
├── portal/                      # Operator portal — separate FastAPI app (see docs/portal-design.md)
│   ├── Dockerfile
│   ├── pyproject.toml
│   └── app/                     # main / config / db (SQLite) / auth / status / routes / templates / static
├── scripts/                     # One-shot tools (backfill, seed, etc.)
├── deploy/launchd/              # macOS launchd plist (llama-server and other host-side services)
├── docker-compose.yml           # Base (backend + postgres + portal, all on proxy-net)
├── docker-compose.dev.yml       # Auto-loaded when TIGERDUCK_ENV=development; publishes ports + swaps to a host bridge
├── _compose-files.sh            # Shared: derives compose -f flags from TIGERDUCK_ENV
├── Dockerfile / entrypoint.sh   # Backend container
├── start.sh / stop.sh / logs.sh / clean-db.sh
├── .env.example
└── pyproject.toml / uv.lock
```

## Contributing

PRs and issues are welcome. Before submitting:
1. `uv run pytest` is green
2. Include an alembic revision if you touch the schema
3. Name your branch `feature/your-feature` or `fix/your-fix`; target the `dev` branch in the PR
4. Spell out the user-visible impact in the PR description (anything that ships to the iOS / Android client)

## License

This project is licensed under the [GNU Affero General Public License v3.0](LICENSE), matching [tigerduck-app](https://github.com/tigerduck-app/tigerduck-app).
