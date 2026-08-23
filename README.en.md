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

TigerDuck Backend is the server side of the [TigerDuck](https://github.com/tigerduck-app/tigerduck-app) app (iOS / Android). It runs at `api.tigerduck.app` and is responsible for five things:

- 🔐 **Accounts & auth (v3)** — NTUST SSO login verification, JWT access + refresh token rotation (theft detection revokes the whole chain), AES-256-GCM credential storage, user device & push-token management
- 🔄 **User data sync (v3)** — Multi-device sync of courses / assignments / settings / bulletin subscriptions: initial client upload + per-user changelog incremental pulls, with the server periodically fetching authoritative Moodle assignment updates
- 📣 **Bulletin pipeline** — Scrape NTUST departmental announcements → de-duplicate → LLM classification (canonical_org / content_tags / importance) → match subscriptions → push (dual-track: anonymous devices and logged-in users)
- 📲 **Push delivery** — Logged-in users go through the two-phase `push_jobs` → `push_deliveries` pipeline (assignment / course reminders, bulletins, system notices); APNs Push-to-Start (iOS Live Activity), FCM fan-out (Android), bad-token classification and cleanup
- ⏰ **Scheduling** — Server-side academic sync, push pipeline, reminder scans, Live Activity tokens, retention cleanup; all driven by a single APScheduler worker running inside the FastAPI lifespan

The service is deliberately **containerised, restart-safe, and stateless**: every bit of state lives in Postgres, so restarting the backend container loses no events and the scheduler simply resumes.

## Modules

### 🔐 Accounts & Auth (`server/auth/`)
- **Login** — The app completes NTUST SSO itself; the backend verifies identity via the Moodle token (it never proxies SSO, avoiding the school's per-IP rate limits)
- **Tokens** — Short-lived JWT access tokens + 90-day refresh token rotation; replay detection revokes the whole session chain plus same-device sessions, with a one-shot 60-second grace retry for clients that lost the rotation response
- **Credential custody** — The NTUST password is stored AES-256-GCM encrypted with per-row AAD (key rotation supported), used only for server-side Moodle token refresh
- **Device management** — `/v3/devices` registers user devices and push tokens (standard / live_activity_update); deleting a device also revokes its sessions and invalidates its tokens

### 🔄 User Sync (`server/sync/`)
- **Client-authoritative mirror** — Courses (incl. schedule_json), assignment snapshots, per-field overrides, settings documents (namespace + revision optimistic concurrency), bulletin subscriptions and read-states
- **Incremental sync** — Per-user changelog: strictly increasing revisions (a row lock makes commit order equal revision order); clients pull deltas with `since_revision`, and an expired cursor returns 410 to trigger a full sync
- **Retention** — Periodic changelog compaction, tracking the compacted revision for the 410 boundary

### 🎓 Server-Side Academic Sync (`server/syncjobs/`)
- **Scheduled fetching** — `sync_policies` (admin-tunable) × `sync_jobs` (provisioned at login) × `sync_runs` (audit); the executor claims work under an advisory lock + `FOR UPDATE SKIP LOCKED`, safe across multiple workers
- **Password iron rule** — An attempt marker is durably committed BEFORE every SSO attempt, so the password is used at most once; auth-class failures are never retried — sync is disabled and a reauth system notification is queued
- **Assignment mirror** — Fetches Moodle assignments, authoritatively upserts / soft-deletes, and writes the changelog so every device converges
- **Pull-to-refresh** — `POST /v3/sync-jobs/run-now` (per-user cooldown; refused while the policy is disabled)

### 📣 Bulletins (`server/bulletins/`)
- **scraper** — Fetches HTML from NTUST bulletin index pages and extracts metadata. NTUST's TLS chain is broken, so we ship a pinned CA bundle (falling back to `verify=False`).
- **dedup** — `content_hash` deduplication (same source + same hash is treated as a repost and marked `skipped`, no re-notification).
- **LLM classification** — OpenAI-compatible client (defaults to a host-side [llama-server](https://github.com/ggml-org/llama.cpp)). Returns `canonical_org` / `content_tags` / `importance` / `title_clean` / `summary` / `body_clean`.
- **Subscription matching + dispatch (dual-track)** — Anonymous devices match `BulletinSubscription` rules through the existing `bulletin_dispatches` path; logged-in users match `user_bulletin_subscriptions` through `bulletin_user_matches` + `push_jobs`. A physical device that logs in gets a `linked_user_id` marker so the anonymous path skips it — no double pushes.
- **State machine** — `pending` → `processed` / `skipped` / `failed`. `failed` rows return to `pending` for another tick as long as attempts < max.

### 📲 Push (`server/push/`)
- **User push pipeline** — `push_jobs` (dedupe keys prevent duplicates) → materialized into per-token `push_deliveries` → APNs / FCM delivery → aggregated to `sent` / `partial_failed` / `failed`; round-based retries and stale-lock recovery
- **Reminder sources** — Assignment reminders (24h / 2h before due, per-user notification settings) and course reminders (class start computed from schedule_json × the NTUST period table, default 10 minutes ahead); submitting / dropping / schedule changes cancel stale reminders
- **APNs** — JWT auth, Push-to-Start, Live Activity update / end
- **FCM** — Batched fan-out, automatic cleanup on `UNREGISTERED` / `SENDER_ID_MISMATCH`
- **Auth** — All v3 routes use `Authorization: Bearer <JWT>`; admin endpoints use `X-Shared-Secret`; bulletin reads are public

### ⏰ Scheduler
- **Single worker** — APScheduler runs inside the FastAPI lifespan; replica count is locked at 1. Multiple replicas would double-send (see [`docs/scheduler.md`](docs/scheduler.md)).
- **Tick design** — Bulletin scrape / process / dispatch (anonymous + user-level), sync jobs, the push pipeline, assignment / course reminder scans, and retention each have their own interval trigger and don't block each other; cross-worker safety comes from DB locks (advisory lock + `SKIP LOCKED`).

## Stack

| Layer | Choice |
|---|---|
| Web | FastAPI 0.115 + Uvicorn + structlog (JSON logs) |
| Operator portal | FastAPI for the API + a React 19 / Vite 8 / Tailwind 4 / TypeScript 7 SPA |
| ORM | SQLAlchemy 2.x async + Alembic |
| DB | Postgres 17 (containerised, internal-only network) |
| Scheduling | APScheduler 3.x (IntervalTrigger) |
| Push | `aioapns` (APNs), `google-auth` + `httpx` (FCM v1) |
| LLM | OpenAI-compatible client → llama-server (host), `response_format: json_object` + JSON schema |
| Deployment | Docker Compose + nginx-proxy-manager |

## Architecture

```
       Public                                           host (macOS / Linux)
   ┌──────────────┐                               ┌────────────────────────────┐
   │  iOS / And.  │ ── HTTPS ──▶ nginx-proxy ───▶ │  tigerduck-internal        │
   └──────────────┘              -manager  ───┐   │  (FastAPI + APScheduler)   │
                                              │   │           │                │
   ┌──────────────┐                           │   │           ├── APNs         │
   │ Operator     │ ── HTTPS ──▶ cloudflared ─┼──▶│  tigerduck-portal          │
   └──────────────┘   (Zero Trust)            │   │  (FastAPI + React, :40010) │
                                              │   │           │                │
                                              │   │           ▼                │
                                              │   │  ┌────────────────┐        │
                                              │   │  │ tigerduck-db   │        │
                                              │   │  │ (Postgres 17)  │        │
                                              │   │  └────────────────┘        │
                                              │   │           ▲                │
                                              │   │           │                │
                                              │   │  ┌────────────────┐        │
                                              │   │  │ llama-server   │        │
                                              │   │  │ (native, Metal)│        │
                                              │   │  └────────────────┘        │
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
| `./clean-db.sh` | **Destructive** — drops the postgres volume; full reset. The portal itself is stateless, so there's no portal volume to preserve |

### Operator portal

`tigerduck-portal` is a sibling compose service that comes up alongside the backend. Its frontend is the React SPA in `portal/web`, compiled to `web/dist` by the Dockerfile's node build stage and served statically by FastAPI; `/api/*` carries the JSON endpoints. Dev mode publishes it at `http://localhost:40010`; production typically lives behind cloudflared / Cloudflare Zero Trust if you want a signin gate (the portal itself does not enforce one). It can:

- Show stack status (every field `./start.sh` prints, plus containers via the docker engine UDS, backend version via `/version`, postgres row counts, LLM reachability, APNs/FCM secret presence, host LAN IPs as clickable links)
- Stream the last N lines of each container's logs with per-tab search; Android / Apple tabs are substring-filtered slices of the backend log
- Export `tigerduck-export-<timestamp>.tar.gz` (custom-format `pg_dump` + manifest); import the same format OR a bare `pg_dump` from a pre-portal install
- Compose and dispatch a custom push to a single device or a named device-list cohort, with payload preview and recent-history view

Full design: [`docs/portal-design.md`](docs/portal-design.md).

### LLM (host side)

The backend talks to a [llama-server](https://github.com/ggml-org/llama.cpp) running natively on the host:

```bash
# Example (a gemma-style instruct small model)
llama-server \
  --hf ggml-org/gemma-4-E4B-it-GGUF \
  --alias gemma-4-e4b-it \
  --host 0.0.0.0 --port 40001 \
  --api-key <your-key> \
  --json-schema '{}'
```

Matching `.env`:

```dotenv
TIGERDUCK_LLM_BASE_URL=http://host.docker.internal:40001/v1
TIGERDUCK_LLM_API_KEY=<your-key>
TIGERDUCK_LLM_MODEL=gemma-4-e4b-it
```

> ⚠️ Models that emit reasoning channels (harmony format, e.g. `<|channel>thought<channel|>`) are **incompatible** — the JSON parser only strips markdown fences, not channel markers. Pick a plain instruct model.

On macOS, `deploy/launchd/ai.tigerduck.llm.plist` wraps llama-server as a launchd service for long-running deployments.

## Retired APIs

`/v1/*` and `/v2/*` are fully retired — every request returns **410 Gone**. Legacy clients must update the app to use `/v3`.

## API Endpoints (v3)

All v3 routes use `Authorization: Bearer <JWT>` unless noted below.

### Auth

| Method | Path | Purpose | Auth |
|---|---|---|---|
| `POST` | `/v3/auth/login` | NTUST SSO login (Moodle token verification) → access + refresh tokens | none |
| `POST` | `/v3/auth/refresh` | Refresh token rotation (replay detection revokes the family) | refresh token |
| `PATCH` | `/v3/auth/credentials` | Update stored encrypted credentials | JWT |
| `POST` | `/v3/auth/logout` | Revoke the current session | JWT |

### Devices

| Method | Path | Purpose | Auth |
|---|---|---|---|
| `POST` | `/v3/devices/register` | Register user device + push token | JWT |
| `GET` | `/v3/devices` | List devices | JWT |
| `DELETE` | `/v3/devices/{id}` | Delete a device (revokes sessions, invalidates tokens) | JWT |
| `PATCH` | `/v3/devices/{id}/preferences` | Update sync preferences (sync_courses / colors / names / assignments) | JWT |

### Sync

| Method | Path | Purpose | Auth |
|---|---|---|---|
| `POST` | `/v3/sync/initial-upload` | Initial upload of local data (courses / assignments / settings / subscriptions) | JWT |
| `GET` | `/v3/sync?since_revision=N` | Changelog incremental sync (expired cursor → 410) | JWT |
| `GET` | `/v3/sync/full` | Full snapshot | JWT |
| `GET` | `/v3/sync/revision` | Current revision number | JWT |
| `POST` | `/v3/sync/courses/upload` | Upload courses | JWT |
| `DELETE` | `/v3/sync/courses` | Delete all courses | JWT |
| `DELETE` | `/v3/sync/courses/{key}` | Delete a single course | JWT |
| `POST` | `/v3/sync/assignments/upload` | Upload assignments | JWT |
| `PATCH` | `/v3/sync/courses/{moodle_id}/override` | Course color/name override (outbox drain) | JWT |
| `PATCH` | `/v3/sync/assignments/{moodle_id}/override` | Assignment status override (outbox drain) | JWT |

### Courses / Assignments

| Method | Path | Purpose | Auth |
|---|---|---|---|
| `GET` | `/v3/courses` | Course list | JWT |
| `PUT` | `/v3/courses/{id}/override` | Course override (authoritative) | JWT |
| `PUT` | `/v3/courses/{id}/skipped-dates/{date}` | Add skipped date | JWT |
| `DELETE` | `/v3/courses/{id}/skipped-dates/{date}` | Remove skipped date | JWT |
| `GET` | `/v3/assignments` | Assignment list | JWT |
| `PUT` | `/v3/assignments/{id}/override` | Assignment status override (authoritative) | JWT |

### Settings

| Method | Path | Purpose | Auth |
|---|---|---|---|
| `GET` | `/v3/settings` | List all namespaces | JWT |
| `GET` | `/v3/settings/{namespace}` | Get settings for a namespace | JWT |
| `PUT` | `/v3/settings/{namespace}` | Update settings (revision CAS, 409 on conflict) | JWT |

### Bulletins

| Method | Path | Purpose | Auth |
|---|---|---|---|
| `GET` | `/v3/bulletins` | Bulletin list (cursor pagination) | JWT |
| `GET` | `/v3/bulletins/{id}` | Bulletin detail | JWT |
| `GET` | `/v3/bulletins/taxonomy` | org / tag label mapping | JWT |
| `GET` | `/v3/bulletin-subscriptions` | List subscription rules | JWT |
| `PUT` | `/v3/bulletin-subscriptions` | Bulk-put subscription rules | JWT |
| `POST` | `/v3/bulletin-subscriptions` | Create subscription rule | JWT |
| `PATCH` | `/v3/bulletin-subscriptions/{id}` | Update subscription rule (base_revision) | JWT |
| `DELETE` | `/v3/bulletin-subscriptions[/{id}]` | Delete subscription rule(s) | JWT |
| `GET` | `/v3/bulletin-states` | Bulletin read / starred / hidden state | JWT |
| `PUT` | `/v3/bulletin-states/{id}` | Set per-bulletin state | JWT |

### Schedule / Live Activity

| Method | Path | Purpose | Auth |
|---|---|---|---|
| `POST` | `/v3/schedule/sync` | Class-table sync (feeds the Live Activity scheduler) | JWT |
| `DELETE` | `/v3/schedule/sync` | Delete schedule data | JWT |
| `POST` | `/v3/live-activities/register` | Register Live Activity update token | JWT |

### Server-side fetch / Admin

| Method | Path | Purpose | Auth |
|---|---|---|---|
| `POST` | `/v3/sync-jobs/run-now` | Pull-to-refresh server fetch trigger (cooldown) | JWT |
| `GET` | `/v3/admin/sync-policies` | List sync policies | shared secret |
| `PATCH` | `/v3/admin/sync-policies/{job_type}` | Update sync policy | shared secret |

### Meta

| Method | Path | Purpose | Auth |
|---|---|---|---|
| `GET` | `/health` | Liveness check | none |
| `GET` | `/version` | Version and API base path | none |

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
│   ├── auth/                    # v3 identity: crypto (credential encryption) / tokens / service / rate_limit / moodle / models
│   ├── sync/                    # v3 user sync: upload / changelog / serializers / retention / models
│   ├── syncjobs/                # Server-side fetching: executor / credentials (password iron rule) / moodle_client / assignments / provisioning
│   ├── routes/                  # v3: auth / user_devices / sync / academics / overrides / settings_docs / bulletins_feed / bulletins_v3 / sync_jobs / schedule_v3 / live_activities_v3
│   ├── push/                    # apns_client / fcm_client / router / pipeline (two-phase delivery) / reminders / course_reminders / job_payloads
│   ├── scheduler/               # APScheduler runtime, dispatch, retention
│   ├── bulletins/               # scraper / dedup / matcher / dispatcher (anonymous) / user_dispatch (logged-in) / taxonomy
│   │   └── llm/                 # OpenAI-compatible client + prompt
│   ├── secrets/                 # APNs .p8 (gitignored)
│   ├── migrations/              # Alembic
│   └── tests/                   # pytest (unit + integration)
├── portal/                      # Operator portal — separate FastAPI app (see docs/portal-design.md)
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── app/                     # FastAPI: main / config / db (asyncpg) / logs / status / routes / static
│   └── web/                     # React 19 + Vite 8 + Tailwind 4 SPA (built into the image as web/dist)
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

This project is licensed under the [GNU Affero General Public License v3.0](LICENSE), matching [tigerduck-app](https://github.com/tigerduck-app/tigerduck-app) and [tigerduck-app-android](https://github.com/tigerduck-app/tigerduck-app-android).
