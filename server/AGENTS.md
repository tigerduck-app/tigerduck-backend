# PUSH NOTIFICATION SERVER KNOWLEDGE BASE

## OVERVIEW
`server/` is the TigerDuck push + bulletin backend. Runs on the Mac mini
behind nginx-proxy-manager + Cloudflare, serves the apps at
`https://api.tigerduck.app/v3/`. **`/v1/*` and `/v2/*` are retired** — a
middleware in `main.py` answers both with 410 Gone, so an out-of-date
client gets an unambiguous "update the app" signal rather than a 404.

What ships today:
- User identity + auth (`routes/auth.py`, JWT). Device identity rides in
  the token rather than in the request body.
- Device registration under `/v3/devices` (`routes/user_devices.py`)
- Sync (courses, assignments, overrides, changelog) plus the server-side
  sync job executor
- Push delivery through `push_jobs` → `push_deliveries` (`push/pipeline.py`)
- Live Activity update / end pushes, built in `push/job_payloads.py`
- FCM fan-out (Android push)
- Bulletin pipeline (scrape → dedup → LLM classify → match → dispatch)
- APScheduler-in-lifespan (single worker — see `docs/scheduler.md`)

## STRUCTURE
```text
server/
├── __init__.py
├── _ssl_compat.py             # OpenSSL 3 leniency for NTUST's broken TLS chain
├── main.py                    # FastAPI app + lifespan; the /v1+/v2 410 middleware
├── config.py                  # pydantic-settings, TIGERDUCK_ prefix
├── db.py                      # Base, async engine/session, SessionDep
├── models.py                  # DeviceRegistration, DeviceList, CustomPushDispatch
├── security.py                # shared-secret dependency (X-Push-Token)
├── system_settings.py         # operator-tunable settings read at runtime
├── logging_setup.py           # structlog console/JSON
├── auth/                      # v3 identity: models, JWT, Moodle creds, cipher
├── sync/                      # v3 sync models + changelog retention
├── syncjobs/                  # server-side academic sync executor
├── routes/
│   ├── auth.py                # /v3/auth/*
│   ├── user_devices.py        # /v3/devices/*
│   ├── sync/                  # /v3/sync/*
│   ├── sync_jobs.py           # /v3/sync-jobs/* (+ admin router)
│   ├── academics.py           # /v3/courses, /v3/assignments
│   ├── overrides.py           # /v3/sync/overrides
│   ├── holiday_overrides.py   # /v3/sync/holiday-overrides
│   ├── academic_calendar.py   # /v3/calendar/*
│   ├── bulletins_feed.py      # /v3/bulletins/*
│   ├── bulletins_v3.py        # subscriptions + read-state
│   ├── schedule_v3.py         # /v3/schedule/*
│   ├── live_activities_v3.py  # /v3/live-activities/register
│   └── settings_docs.py       # operator-facing settings docs
├── push/
│   ├── pipeline.py            # push_jobs -> push_deliveries worker (THE send path)
│   ├── job_payloads.py        # build_apns_for_job / build_fcm_for_job
│   ├── payload.py             # alert + custom-push builders, ApnsRequest/FcmRequest
│   ├── apns_client.py         # AioApnsSender + RecordingSender (factory)
│   ├── fcm_client.py          # FCM v1 sender + RecordingFcmSender
│   ├── router.py              # platform routing for outbound pushes
│   ├── reminders.py           # assignment reminder scan
│   ├── course_reminders.py    # course reminder scan
│   ├── custom_push_*.py       # operator-authored pushes: targeting + dispatch
│   └── retention.py           # prune terminal push_jobs
├── bulletins/
│   ├── scraper.py             # NTUST HTML → metadata
│   ├── dedup.py               # content_hash gating
│   ├── detail.py / models.py / schemas.py / taxonomy.py
│   ├── matcher.py             # subscription-rule evaluation
│   ├── dispatcher.py          # outbound push fan-out (anonymous devices)
│   ├── user_dispatch.py       # fan-out for logged-in users
│   ├── jobs.py                # APScheduler tick handlers
│   └── llm/                   # OpenAI-compatible client + prompts
├── scheduler/
│   └── runtime.py             # APScheduler bootstrap (in FastAPI lifespan)
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
- `dedupe_key` on a `push_job` is what makes client retries idempotent —
  the partial unique index only covers active statuses, so an ON CONFLICT
  must repeat that predicate or Postgres cannot match the index.
- Two kinds of Live Activity job share the `schedule` channel, told apart
  by `payload["kind"]`. A start (`schedule`, from `/schedule/sync`) goes to
  the device's `push_to_start` token as an `event: start` push carrying
  `attributes-type` + `attributes` + `alert`; an end (`live_activity_end`,
  from `/live-activities/register`) goes to that activity's own
  `live_activity_update` token (scoped by `scope_key` = activity id).
  Neither token can do the other's job. `activity_id` is
  `"{scenario}::{source_id}"` — the client's `composedActivityId` — and a
  start is cancelled (`activity_already_running`) while the device's
  `la_end:{device_id}:{activity_id}` job is still waiting to fire, but only
  before the job's first delivery row exists (a retry round must not
  cancel a push that already went out). A push-to-start token's
  `scope_key` is the `ActivityAttributes` type name the client registered
  it under, and the start push names it as `attributes-type`.
- Both Live Activity job keys live in `push/dedupe.py` and are filed per
  device: `schedule:{device_id}:{source_id}:{scenario}` for a start,
  `la_end:{device_id}:{activity_id}` for an end. `/schedule/sync`,
  `/live-activities/register` and a semester-scoped `DELETE
  /sync/courses` all refuse a session without a device id. Neither key
  carries an occurrence because the client's `source_id` already does
  (`{course_no}_{yyyyMMdd}_{period}`); a re-sync of the same occurrence
  finds its own sent job and leaves it alone.
- An FCM token is `standard` only; Live Activity token kinds are APNs.
- Registering a push token retires the device's other active tokens of
  the same kind (`_retire_superseded_tokens`; scope-narrowed only for
  Live Activity update tokens); a rotated token must not keep its
  predecessor delivering.
- `/sync/full` tags each course tombstone with `deleted_by_reset` and
  `deleted_by_this_device`; a client ignores a tombstone that is both,
  the same rule `upload_courses` applies when it releases them.
- APNs topic for a Live Activity: `{bundle_id}.push-type.liveactivity`
  (the payload builder handles this — do not hardcode elsewhere)
- Scheduler runs IN-PROCESS in FastAPI's lifespan as a single worker.
  Never spin up a second replica — see `docs/scheduler.md`.

## ANTI-PATTERNS
- Do not handle Moodle/NTUST credentials outside `server/auth/`.
  v3 keeps the Moodle token (never the NTUST password) encrypted at
  rest via `CredentialCipher` so the server-side sync job can fetch on
  the user's behalf; it is decrypted only in `server/syncjobs/credentials.py`,
  never logged, and dropped with the account. Anything else that needs a
  credential goes through that path.
- Do not send APNs pushes from inside request handlers. Scheduling
  goes through a `push_jobs` row and is delivered by the push pipeline
  (`server/push/pipeline.py`) on its APScheduler tick.
- Do not commit `.env`, `server/secrets/*.p8`, or anything under
  `server/migrations/versions/` without reviewing first (migrations are
  fine to commit; the warning is to avoid accidentally committing test
  SQL dumps).
- Do not use the standard `apns-topic: {bundle_id}` for Live
  Activities; iOS will silently drop the push.
- Do not commit `docker-compose.override.yml` — it's the gitignored
  per-machine tweak file. The canonical dev override is the committed
  `docker-compose.dev.yml`.
- Do not add app-level auth (basic-auth, sessions, JWT…) to the
  portal. The portal is stateless and trusts whatever is in front of
  it (Cloudflare Zero Trust in prod, nothing in dev). Add an
  auth-proxy if a gate becomes necessary, rather than re-introducing
  an admin list / session store inside the portal.
- Do not let the portal drive backend lifecycle (start/stop/restart).
  Its docker socket mount is read-only on purpose; container lifecycle
  belongs to the host operator running `./start.sh`. The import flow
  shows the user a "restart manually" banner instead.
