# PUSH NOTIFICATION SERVER KNOWLEDGE BASE

## OVERVIEW
`server/` is the TigerDuck push + bulletin backend. Runs on the Mac mini
behind nginx-proxy-manager + Cloudflare, serves the iOS app at
`https://api.tigerduck.app/v2/`. `/v1/*` is mounted as a deprecated alias
for older iOS builds (see `MIGRATE.md` § API version compatibility).

What ships today:
- Device registration (APNs + FCM tokens, per-device subscription rules)
- Schedule sync (client-authoritative list of next-48h events; drives
  Live Activity Push-to-Start)
- APNs payload builder + sender (Push-to-Start, update, end)
- FCM fan-out (Android push)
- Bulletin pipeline (scrape → dedup → LLM classify → match → dispatch)
- APScheduler-in-lifespan (single worker — see `docs/scheduler.md`)
- Live Activity update-token push

## STRUCTURE
```text
server/
├── __init__.py
├── _ssl_compat.py             # OpenSSL 3 leniency for NTUST's broken TLS chain
├── main.py                    # FastAPI app + lifespan (LLM probe, engine, scheduler)
├── config.py                  # pydantic-settings, TIGERDUCK_ prefix
├── db.py                      # Base, async engine/session, SessionDep
├── models.py                  # DeviceRegistration, ScheduledPush, etc.
├── schemas.py                 # Pydantic request/response (cross-route)
├── security.py                # shared-secret dependency (X-Push-Token)
├── logging_setup.py           # structlog console/JSON
├── routes/
│   ├── devices.py             # /v2/devices/{register,unregister,…}
│   ├── schedule.py            # /v2/schedule/sync, …
│   ├── bulletins.py           # /v2/bulletins/{list,detail,taxonomy}
│   ├── live_activities.py     # /v2/live-activities/start-tokens
│   └── debug.py               # debug-only routes
├── push/
│   ├── payload.py             # build_apns_request, build_pts_payload
│   ├── apns_client.py         # AioApnsSender + RecordingSender (factory)
│   ├── fcm_client.py          # FCM v1 sender + RecordingFcmSender
│   └── router.py              # platform routing for outbound pushes
├── bulletins/
│   ├── scraper.py             # NTUST HTML → metadata
│   ├── dedup.py               # content_hash gating
│   ├── detail.py / models.py / schemas.py / taxonomy.py
│   ├── matcher.py             # subscription-rule evaluation
│   ├── dispatcher.py          # outbound push fan-out
│   ├── jobs.py                # APScheduler tick handlers
│   └── llm/                   # OpenAI-compatible client + prompts
├── scheduler/
│   ├── runtime.py             # APScheduler bootstrap (in FastAPI lifespan)
│   ├── dispatcher.py          # Live Activity / scheduled-push tick
│   └── retention.py           # cleanup tick (bulletins, dead tokens)
├── migrations/                # Alembic, async template
├── secrets/                   # .p8 / fcm_service_account.json (gitignored)
└── tests/                     # pytest-asyncio integration + unit tests
```

## RUNNING

### Local dev (Docker compose, dev override)

```bash
cd tigerduck-backend
cp .env.example .env          # defaults to TIGERDUCK_ENV=development
./start.sh                    # auto-loads docker-compose.dev.yml, prints status block
```

`./start.sh` reads `TIGERDUCK_ENV` from `.env`. When it's `development`
the script appends `-f docker-compose.dev.yml`, which publishes
backend `:40000` + portal `:40010` to the host (via a non-internal
bridge — see the dev override file for why) and drops the prod-only
`proxy-net`. See `docs/local-dev-backend.md` for the full first-time
setup.

Health checks from outside the container (only work in dev where the
ports are published):
```bash
curl -sS http://localhost:40000/health   # backend
curl -sS http://localhost:40010/health   # portal
```

The portal status page at `http://localhost:40010/` is the
canonical place to see "is everything actually up?" — it queries the
docker engine over the mounted UDS for each container's state, plus
postgres ping, LLM ping, and APNs/FCM secret presence in one render.

### Production (Docker compose, NPM-fronted)

```bash
cd tigerduck-backend
cp .env.example .env          # set TIGERDUCK_ENV=production + real secrets
./start.sh                    # uses only docker-compose.yml; proxy-net required
```

nginx-proxy-manager (on `proxy-net`) routes `api.tigerduck.app` to
`http://tigerduck-internal:40000`. No ports are published to the host;
postgres is private to `tigerduck-db` and unreachable from outside the
backend container.

`llama-server` stays NATIVE on the host (Docker on Mac can't get Metal
GPU). The backend reaches it via `host.docker.internal:40001`. See
`deploy/launchd/ai.tigerduck.llm.plist` for the launchd service.

### One-shot backfill
```bash
# The production image only bundles .venv (no uv). Call python directly —
# /app/.venv/bin is on PATH so it resolves to the pinned 3.13.
docker compose exec backend \
  python scripts/backfill_bulletins.py --pages 20 --concurrency 3
```

### Tests (host-side, not inside the container)
```bash
uv sync
uv run pytest server/tests/ -v   # LLM + pipeline tests; DB-backed ones need postgres up
```

## CONFIG
All env vars use the `TIGERDUCK_` prefix. See `.env.example` for the
full, commented list. Drop APNs credentials under
`server/secrets/AuthKey_<KEY_ID>.p8` — the path is mounted read-only
into the backend container via `docker-compose.yml`.

## CONVENTIONS
- All DB timestamps stored as `timestamp with time zone` (UTC in, UTC out)
- Session management: `SessionDep` in routes auto-commits/rolls-back
- `push_id = f"{device_id}:{source_id}:{scenario}"` — deterministic so client
  UPSERTs are idempotent
- `/v2/schedule/sync` = full replacement per device (pending pushes not
  in the payload get cancelled; sent/failed history preserved)
- APNs topic for PTS: `{bundle_id}.push-type.liveactivity` (payload
  builder handles this — do not hardcode elsewhere)
- Scheduler runs IN-PROCESS in FastAPI's lifespan as a single worker.
  Never spin up a second replica — see `docs/scheduler.md`.

## ANTI-PATTERNS
- ❌ Do not store user Moodle/NTUST credentials in this service. The
  design assumes the client owns credentials; the server only knows
  schedule metadata.
- ❌ Do not send APNs pushes from inside request handlers. Scheduling
  goes through `scheduled_pushes` and is dispatched by the APScheduler
  tick.
- ❌ Do not commit `.env`, `server/secrets/*.p8`, or anything under
  `server/migrations/versions/` without reviewing first (migrations are
  fine to commit; the warning is to avoid accidentally committing test
  SQL dumps).
- ❌ Do not use the standard `apns-topic: {bundle_id}` for Live
  Activities; iOS will silently drop the push.
- ❌ Do not commit `docker-compose.override.yml` — it's the gitignored
  per-machine tweak file. The canonical dev override is the committed
  `docker-compose.dev.yml`.
- ❌ Do not add app-level auth (basic-auth, sessions, JWT…) to the
  portal. The portal trusts whatever is in front of it (Cloudflare Zero
  Trust in prod, nothing in dev). The `admins` table + audit log are
  record-keeping, not a gate; if a gate becomes necessary, flip
  `require_admin` to enforce the admin-list check instead of layering a
  parallel auth system inside the app.
- ❌ Do not let the portal drive backend lifecycle (start/stop/restart).
  Its docker socket mount is read-only on purpose; container lifecycle
  belongs to the host operator running `./start.sh`. The import flow
  shows the user a "restart manually" banner instead.
