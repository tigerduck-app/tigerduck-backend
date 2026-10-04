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
- 🔄 **User data sync (v3)** — Multi-device sync of courses / assignments / settings: initial client upload + per-user changelog incremental pulls, with the server periodically fetching authoritative Moodle assignment updates
- 📣 **Bulletin pipeline** — Scrape NTUST departmental announcements → de-duplicate → LLM classification (canonical_org / content_tags / importance) → match subscriptions → push (dual-track: anonymous devices and logged-in users)
- 📲 **Push delivery** — Logged-in users go through the two-phase `push_jobs` → `push_deliveries` pipeline (assignment / course reminders, bulletins, system notices); APNs Push-to-Start (iOS Live Activity), FCM fan-out (Android), bad-token classification and cleanup
- ⏰ **Scheduling** — Server-side academic sync, push pipeline, reminder scans, Live Activity tokens, retention cleanup; all driven by a single APScheduler worker running inside the FastAPI lifespan

The service is deliberately **containerised, restart-safe, and stateless**: every bit of state lives in Postgres, so restarting the backend container loses no events and the scheduler simply resumes.

## Modules

### 🔐 Accounts & Auth (`server/auth/`)
- **Login** — The app completes NTUST SSO itself; the backend verifies identity via the Moodle token (it never proxies SSO, avoiding the school's per-IP rate limits)
- **Tokens** — Short-lived JWT access tokens + 90-day refresh token rotation; replay detection revokes the whole session chain plus same-device sessions, with a one-shot 60-second grace retry for clients that lost the rotation response
- **Credential custody** — The NTUST password is stored AES-256-GCM encrypted with per-row AAD (key rotation supported), used only for server-side Moodle token refresh
- **Device management** — `/v3/devices` registers user devices and push tokens (standard / live_activity_update); deleting a device also revokes its sessions and invalidates its tokens; devices report their locale and hardware model and carry their own sync and push switches (assignment reminders, Live Activity, bulletin push, …)

### 🔄 User Sync (`server/sync/`)
- **Client-authoritative mirror** — Courses (incl. schedule_json), assignment snapshots, per-field overrides, settings documents (namespace + revision optimistic concurrency), bulletin read-states
- **Incremental sync** — Per-user changelog: strictly increasing revisions (a row lock makes commit order equal revision order); clients pull deltas with `since_revision`, and an expired cursor returns 410 to trigger a full sync
- **Retention** — Periodic changelog compaction, tracking the compacted revision for the 410 boundary

### 🎓 Server-Side Academic Sync (`server/syncjobs/`)
- **Scheduled fetching** — `sync_policies` (admin-tunable) × `sync_jobs` (provisioned at login) × `sync_runs` (audit); the executor claims work under an advisory lock + `FOR UPDATE SKIP LOCKED`, safe across multiple workers
- **Password iron rule** — An attempt marker is durably committed BEFORE every SSO attempt, so the password is used at most once; auth-class failures are never retried — sync is disabled and a reauth system notification is queued
- **Assignment mirror** — Fetches Moodle assignments, authoritatively upserts / soft-deletes, and writes the changelog so every device converges
- **Submission status** — After each assignment sync, asks Moodle whether the assignments due within the window (48 hours by default) are submitted; a newly submitted one has its pending reminder withdrawn and its Live Activity countdown ended
- **Pull-to-refresh** — `POST /v3/sync-jobs/run-now` (per-user cooldown; refused while the policy is disabled)

### 📣 Bulletins (`server/bulletins/`)
- **scraper** — Fetches HTML from NTUST bulletin index pages and extracts metadata. NTUST's TLS chain is broken, so we ship a pinned CA bundle (falling back to `verify=False`).
- **dedup** — `content_hash` deduplication (same source + same hash is treated as a repost and marked `skipped`, no re-notification).
- **LLM classification** — OpenAI-compatible client (defaults to a host-side [llama-server](https://github.com/ggml-org/llama.cpp)). Returns `canonical_org` / `content_tags` / `importance` / `title_clean` / `summary` / `body_clean`.
- **Subscription matching + dispatch (dual-track)** — Anonymous devices match `BulletinSubscription` rules through the existing `bulletin_dispatches` path; logged-in users match `user_bulletin_subscriptions` (rules belong to each device and sit outside TigerSync) through `bulletin_user_matches` + `push_jobs`, and only devices whose rules matched and that have not turned bulletin push off receive it. A physical device that logs in gets a `linked_user_id` marker so the anonymous path skips it — no double pushes.
- **State machine** — `pending` → `processed` / `skipped` / `failed`. `failed` rows return to `pending` for another tick as long as attempts < max.

### 📲 Push (`server/push/`)
- **User push pipeline** — `push_jobs` (dedupe keys prevent duplicates) → materialized into per-token `push_deliveries` → APNs / FCM delivery → aggregated to `sent` / `partial_failed` / `failed`; round-based retries and stale-lock recovery
- **Reminder sources** — Assignment reminders (lead times from the notification settings document, sent only to iPhones and iPads with reminder sync on) and course reminders (class start computed from schedule_json × the NTUST period table, default 10 minutes ahead); submitting / dropping / schedule changes cancel stale reminders
- **School holidays** — Course reminders and the class Live Activities (`classPreparing` / `inClass`) are not sent on an academic holiday (`academic_holidays`) unless the user opted in to that holiday with "Still have class?" (`user_holiday_overrides`). The app files Live Activity starts ahead of time, so the check runs **when a start fires**: a holiday published later, or an opt-in changed later, still applies to jobs already queued, and a held job ends `cancelled` with `last_error = holiday`. Assignment Live Activities ignore holidays
- **Localized copy** — Assignment reminder and reauth notification text is built at delivery time in each device's language, from app-translation
- **APNs** — JWT auth, Push-to-Start, Live Activity update / end
- **FCM** — Batched fan-out, automatic cleanup on `UNREGISTERED` / `SENDER_ID_MISMATCH`
- **Auth** — All v3 routes use `Authorization: Bearer <JWT>`; admin endpoints use `X-Shared-Secret`; bulletin reads are public

### ⏰ Scheduler
- **Single worker** — APScheduler runs inside the FastAPI lifespan; replica count is locked at 1. Multiple replicas would double-send.
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
| Monitoring | Prometheus + Grafana + Loki (Grafana Alloy), postgres-exporter, sql_exporter |

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
- **`tigerduck-monitoring`**: internal-only bridge for Prometheus, Loki and what they collect from. Grafana is the one monitoring service on `proxy-net`; see [Monitoring](#monitoring-grafana).
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

All four scripts read `TIGERDUCK_ENV` from `.env`; when it's `development` they additionally load `docker-compose.dev.yml` (publishes backend `:40000` + portal `:40010` + Grafana `:40020` + Prometheus `:40021` to the host via a non-internal bridge network — the prod-only `proxy-net` is dropped because there's no NPM locally). The mode lives in `.env`; the scripts pick the right compose files automatically.

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
- Per device: its sync switches and synced sections, bulletin subscriptions, hardware model and queued push jobs; the Tests sections send a test reauth notice or Live Activity
- Show who signed in through Cloudflare Access, with a Sign out link that ends their Access session. Dev and LAN visits have no Access session, so the link stays hidden there

### Monitoring (Grafana)

Grafana, Prometheus and Loki come up with the rest of the stack; their config lives in `monitoring/`. They keep the history the portal doesn't: who is online, sync and push health over time, request rates, every container's logs.

| | Development | Production |
|---|---|---|
| Grafana | `http://localhost:40020` (and the LAN IPs `./start.sh` prints), no sign-in | `https://portal.<your-domain>/grafana/`, signed in by Cloudflare Access |
| Prometheus | `http://localhost:40021` | Internal only; query it from Grafana |

The dashboards live in the **TigerDuck** folder; the TigerDuck menu at the top right of each one switches between them.

| Dashboard | What's on it |
|---|---|
| Userbase | Registered users (by student ID) and devices (iPhone + iPad / macOS / Android) against those active in the last 14 days, as line charts with the current numbers above. Rebuilt from the database, so the history reaches back to launch |
| Overview | Online and active devices, every scrape target's health, request rate and errors, push delay, overdue sync jobs, the last bulletin scrape, recent errors |
| Devices | Everything on the portal's Devices tab (device table, platform / app / OS / model breakdown, lists), plus online (seen within N minutes) and active (seen within 14 days) devices over time. Filters for platform, app version, both windows, and a search box |
| Moodle sync | Sync policies, per-student jobs, runs and their durations, failure reasons, NTUST accounts that need to sign in again, the per-user activity log |
| Push | The push_jobs queue, deliveries by provider, APNs / FCM send time, failures, custom pushes |
| Bulletins | Scrape freshness, the LLM backlog and its failures, new bulletins per unit, matches and pushes |
| Logs | Every container's logs from Loki (30 days), by service and level, with text search |
| Backend API | Requests by route and status, latency, scheduler job runs and durations, the SQLAlchemy pool, CPU and memory |
| Postgres | Connections by state and client, live sessions, locks, transactions, table sizes |

Where the numbers come from:

- **postgres-exporter**: Postgres server stats.
- **sql-exporter**: app counts taken with SQL every 30 seconds (devices online, the push queue, sync jobs, …) so they have a history. The queries are in `monitoring/sql-exporter/collectors/`.
- **Backend `:9000/metrics`** (`server/metrics.py`): HTTP, scheduler, DB pool, and APNs / FCM / LLM timings. The port is bound to the backend's address on the internal `tigerduck-monitoring` network only (`TIGERDUCK_METRICS_HOST`), so nothing on `proxy-net` reaches it, and the public `:40000` never answers `/metrics`.
- **Grafana's SQL datasource**: table panels read the database directly as the read-only `tigerduck_monitor` role, which the one-shot `monitor-role` service creates or refreshes on every `./start.sh`. The role has its own password (`TIGERDUCK_MONITOR_DB_PASSWORD`, which `./start.sh` generates into `.env` when missing) and a 30-second statement timeout, so a heavy query can't hold locks a migration is waiting for.
- **Alloy → Loki**: every container's output, read through the docker socket.

Charts drawn from Prometheus start on the day monitoring is first deployed; SQL panels show whatever is in the tables.

**Production setup**

1. In `.env`, set `TIGERDUCK_CF_ACCESS_TEAM_DOMAIN` and `TIGERDUCK_CF_ACCESS_AUD`, the AUD tag of the portal's Access application. Grafana has no login of its own: it signs people in from the `Cf-Access-Jwt-Assertion` token Access adds to every request, after checking its signature, issuer and audience. A request that reaches Grafana without going through Access (LAN straight to NPM, another container on `proxy-net`) is refused, and with the two values unset nobody gets in.
2. Route `/grafana` on the portal host to `tigerduck-grafana:3000`, keeping the `/grafana` prefix:
   - nginx-proxy-manager: on the portal's proxy host, add a Custom Location `/grafana` → `http`, `tigerduck-grafana`, `3000`, with no path after the host. For Grafana Live, add `proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection "upgrade";` to the location's advanced config.
   - cloudflared pointing straight at the portal: add an ingress rule above the portal's, with `hostname: portal.<your-domain>`, `path: ^/grafana`, `service: http://tigerduck-grafana:3000`.
3. Nothing to change in Cloudflare Access: the application on the portal host already covers `/grafana`, as long as its path is left empty. Signing out of the portal signs out of Grafana too.

**Changing things**

- The dashboards in `monitoring/grafana/dashboards/` are generated by `monitoring/grafana/build_dashboards.py`, where filters and queries shared by many panels are defined once; they can't be saved from the UI. Edit the script and run `python3 monitoring/grafana/build_dashboards.py` (Python 3, nothing else); Grafana picks the JSON up within 30 seconds. A panel tried out in the UI has to be ported back into the script. Dashboards made from scratch in the UI are kept in the Grafana volume.
- A new app metric with history: add a query to one of `monitoring/sql-exporter/collectors/*.collector.yml`.
- Prometheus re-reads `prometheus.yml` within 30 seconds of a change, and Grafana its dashboards. Everything else in `monitoring/` is read at startup: after editing it, restart the service it belongs to, e.g. `docker compose restart grafana` (or `sql-exporter`, `loki`, `alloy`).
- Prometheus and Loki both keep 30 days.
- Versions are pinned: every monitoring image by tag and digest, and Grafana's plugins in `GF_PLUGINS_PREINSTALL`, so restarts never pull anything new. The comments in `docker-compose.yml` say how to upgrade.

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
| `PATCH` | `/v3/devices/{id}/preferences` | Update device preferences (synced sections, reminder / Live Activity sync, bulletin and server push switches, locale) | JWT |

### Sync

| Method | Path | Purpose | Auth |
|---|---|---|---|
| `POST` | `/v3/sync/initial-upload` | Initial upload of local data (courses / assignments / overrides / settings) | JWT |
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
| `GET` | `/v3/bulletin-subscriptions` | List this device's subscription rules | JWT |
| `PUT` | `/v3/bulletin-subscriptions` | Bulk-put this device's subscription rules | JWT |
| `POST` | `/v3/bulletin-subscriptions` | Create a subscription rule for this device | JWT |
| `PATCH` | `/v3/bulletin-subscriptions/{id}` | Update subscription rule (base_revision) | JWT |
| `DELETE` | `/v3/bulletin-subscriptions[/{id}]` | Delete subscription rule(s) | JWT |
| `GET` | `/v3/bulletin-states` | Bulletin read / starred / hidden state | JWT |
| `PUT` | `/v3/bulletin-states/{id}` | Set per-bulletin state | JWT |

### Academic calendar

| Method | Path | Purpose | Auth |
|---|---|---|---|
| `GET` | `/v3/calendar/semesters` | Term dates and school holidays (ETag) | none |
| `GET` | `/v3/sync/holiday-overrides` | The user's holiday exceptions ("Still have class?") | JWT |
| `PUT` | `/v3/sync/holiday-overrides/{holiday_id}` | Set whether one holiday still gets class notifications | JWT |

### Schedule / Live Activity

| Method | Path | Purpose | Auth |
|---|---|---|---|
| `POST` | `/v3/schedule/sync` | Class-table sync (feeds the Live Activity scheduler; class starts are checked against school holidays when they fire) | JWT |
| `DELETE` | `/v3/schedule/{source_id}` | Cancel this device's pending starts for one source | JWT |
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
│   ├── metrics.py               # Prometheus metrics, served on :9000 (never the public :40000)
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
├── portal/                      # Operator portal — separate FastAPI app
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── app/                     # FastAPI: main / config / db (asyncpg) / logs / status / routes / static
│   ├── tests/                   # pytest, no database needed
│   └── web/                     # React 19 + Vite 8 + Tailwind 4 SPA (built into the image as web/dist)
├── monitoring/                  # Grafana (provisioning + dashboards), Prometheus, Loki, Alloy, sql-exporter collectors
├── scripts/                     # One-shot tools (backfill, seed, etc.)
├── deploy/launchd/              # macOS launchd plist (llama-server and other host-side services)
├── docker-compose.yml           # Base (backend + postgres + portal on proxy-net, plus the monitoring services)
├── docker-compose.dev.yml       # Auto-loaded when TIGERDUCK_ENV=development; publishes ports + swaps to a host bridge
├── _compose-files.sh            # Shared: derives compose -f flags from TIGERDUCK_ENV
├── Dockerfile / entrypoint.sh   # Backend container
├── start.sh / stop.sh / logs.sh / clean-db.sh
├── .env.example
└── pyproject.toml / uv.lock
```

## Contributing

PRs and issues are welcome. Before submitting:
1. `uv run pytest` is green, and so is `cd portal && uv run pytest` if you touched the portal's Python
2. If you touched the portal frontend, `cd portal/web && npm run build` passes too — the `tsc -b` typecheck is the part that matters
3. Include an alembic revision if you touch the schema
4. Name your branch `feature/your-feature` or `fix/your-fix`; target the `dev` branch in the PR
5. Spell out the user-visible impact in the PR description (anything that ships to the iOS / Android client)

Items 1 and 2 run automatically via `.github/workflows/ci.yaml` on every PR to `dev` / `main`.

## License

This project is licensed under the [GNU Affero General Public License v3.0](LICENSE), matching [tigerduck-app](https://github.com/tigerduck-app/tigerduck-app) and [tigerduck-app-android](https://github.com/tigerduck-app/tigerduck-app-android).
