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
- APScheduler-in-lifespan (single worker)

## STRUCTURE
```text
server/
├── __init__.py
├── _ssl_compat.py             # OpenSSL 3 leniency for NTUST's broken TLS chain
├── main.py                    # FastAPI app + lifespan; the /v1+/v2 410 middleware
├── config.py                  # pydantic-settings, TIGERDUCK_ prefix
├── db.py                      # Base, async engine/session, SessionDep;
│                              #   in_array and INSERT_CHUNK_ROWS (bind limit)
├── models.py                  # DeviceRegistration, DeviceList, CustomPushDispatch
├── security.py                # shared-secret dependency (X-Push-Token) for the admin endpoints
├── system_settings.py         # operator-tunable settings read at runtime
├── admin_audit.py             # admin_audit_log (the portal writes it) + retention
├── tls_pins/                  # the apps' signed TLS pin document: models, document
│                              #   (the only code that signs), watcher, routes
├── logging_setup.py           # structlog console/JSON
├── i18n.py                    # localized push copy from the app-translation bundles
├── metrics.py                 # Prometheus metrics, served on their own :9000 (not :40000)
├── auth/                      # v3 identity: models, JWT, Moodle creds, cipher
├── sync/                      # v3 sync models + changelog retention
├── syncjobs/                  # server-side academic sync executor
├── academic_calendar/         # semester terms + school holidays (models, schemas,
│                              #   terms.py: the current term and the days a term
│                              #   runs on, as the apps take them)
├── routes/
│   ├── auth.py                # /v3/auth/*
│   ├── user_devices.py        # /v3/devices/*
│   ├── sync/                  # /v3/sync/*
│   ├── sync_jobs.py           # /v3/sync-jobs/* (+ admin router)
│   ├── academics.py           # /v3/courses, /v3/assignments
│   ├── overrides.py           # /v3/sync/{courses,assignments}/{id}/override
│   ├── holiday_overrides.py   # /v3/sync/holiday-overrides
│   ├── academic_calendar.py   # /v3/calendar/*
│   ├── bulletins_feed.py      # /v3/bulletins/*
│   ├── bulletins_v3.py        # subscriptions + read-state
│   ├── schedule_v3.py         # /v3/schedule/*
│   ├── live_activities_v3.py  # /v3/live-activities/register
│   └── settings_docs.py       # /v3/settings/*: the user's settings docs (revision CAS)
├── push/
│   ├── pipeline.py            # push_jobs -> push_deliveries worker (THE send path)
│   ├── job_payloads.py        # build_apns_for_job / build_fcm_for_job, incl. the
│   │                          #   portal's pushes (push_jobs on channel `custom`)
│   ├── payload.py             # alert + custom-push builders, thread-ids, ApnsRequest/FcmRequest
│   ├── dedupe.py              # Live Activity job keys + LIVE_ACTIVITY_JOB_PRIORITY
│   ├── apns_client.py         # AioApnsSender + RecordingSender (factory)
│   ├── fcm_client.py          # firebase-admin sender + RecordingFcmSender
│   ├── router.py              # platform routing for outbound pushes
│   ├── reminders.py           # assignment reminder scan
│   ├── presend.py             # just before an assignment reminder or countdown start:
│   │                          #   is the assignment still open? (rows, then Moodle)
│   ├── client_versions.py     # app versions that still schedule assignment reminders locally
│   ├── submission_cancel.py   # on submission: cancel reminders, end Live Activity countdowns
│   ├── sync_trigger.py        # the silent push asking devices to sync after a server write
│   ├── notification_copy.py   # per-recipient copy: reauth, assignment and class
│   │                          #   reminders
│   ├── session_reauth.py      # asks a device left without a session to sign in again
│   ├── course_reminders.py    # course reminder scan
│   ├── custom_push_targeting.py   # device filters; used by the anonymous bulletin dispatcher
│   ├── custom_push_dispatcher.py  # drains the legacy custom_push_dispatches table on a
│   │                              #   tick; nothing writes that table any more
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
backend `:40000` + portal `:40010` + Grafana `:40020` (no sign-in) +
Prometheus `:40021` to the host (via a non-internal bridge — see the dev
override file for why) and drops the prod-only `proxy-net`. See the
README's Deployment section for the full first-time setup.

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
`http://tigerduck-internal:40000`. No ports are published to the host.
Postgres sits on the internal-only `tigerduck-db` network, shared with
the backend, the portal, the one-shot `monitor-role`, `postgres-exporter`,
`sql-exporter` and Grafana; nothing outside those services reaches it.
Grafana is the only monitoring service on `proxy-net`, served at
`<portal host>/grafana` behind Cloudflare Access (it checks the Access
JWT itself).

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
uv run pytest server/tests/ -v
# DB-backed tests need a Postgres the host can reach: localhost:5432
# (tigerduck / tigerduck) by default, or TIGERDUCK_TEST_DATABASE_URL (see
# tests/conftest.py). The stack's postgres is not it: it publishes no port.
# They create and drop their own tigerduck_test database, e.g. on
#   docker run -d --rm -p 127.0.0.1:5432:5432 -e POSTGRES_USER=tigerduck \
#     -e POSTGRES_PASSWORD=tigerduck -e POSTGRES_DB=tigerduck postgres:17-alpine
```

## CONFIG
All env vars use the `TIGERDUCK_` prefix. See `.env.example` for the
full, commented list. Drop APNs credentials under
`server/secrets/AuthKey_<KEY_ID>.p8` — the path is mounted read-only
into the backend container via `docker-compose.yml`.

## CONVENTIONS
- All DB timestamps stored as `timestamp with time zone` (UTC in, UTC out)
- Session management: `SessionDep` in routes auto-commits/rolls-back,
  and commits before the response is sent
- Scheduled work and the workers (`SyncWorker`, `PushPipelineWorker`) use
  a second engine with its own pool; API requests use `app.state.engine`.
  A transaction that sends pushes before it commits, or a retention purge,
  calls `lift_timeouts` (`db.py`) first: the server-side statement and
  idle-in-transaction timeouts would otherwise cut it off. No request
  handler may hold a transaction open across a call to Moodle, APNs or
  FCM; commit first.
- asyncpg refuses a statement with more than 32,767 bind parameters. A
  filter on ids gathered across users goes through `in_array` (`db.py`),
  one array parameter, rather than `in_()`, which binds one per value, and
  rows inserted for every user or device go `INSERT_CHUNK_ROWS` (`db.py`)
  at a time.
- `dedupe_key` on a `push_job` is what makes client retries idempotent —
  the partial unique index only covers active statuses, so an ON CONFLICT
  must repeat that predicate or Postgres cannot match the index. Pass it as
  SQL text (`PUSH_JOB_DEDUPE_ACTIVE_WHERE`, and `PUSH_TOKEN_ACTIVE_WHERE` for
  `ux_push_token_active`), not as `status.in_(...)`: a bound value there
  fails with 42P10 once Postgres plans the statement generically, which the
  tests do from the start.
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
  finds its own sent job and leaves it alone. A still-pending start takes
  the re-synced event's fire time and snapshot, and is cancelled when the
  new fire time has passed.
- Every Live Activity start and end is filed at `push_jobs.priority` 10
  (`LIVE_ACTIVITY_JOB_PRIORITY` in `push/dedupe.py`); other jobs keep the
  default 100, and the pipeline claims the lowest first.
- The push tick runs every 5s (`push_pipeline_tick_seconds`) and drains
  every due job, a batch at a time. Between jobs it checks for a due job
  that outranks the next one, and if one is waiting hands the rest of the
  batch back and claims again. `POST /push-tick` (X-Push-Token)
  forces one: it waits up to 20s, then answers `done: false` and lets the
  tick finish on its own; a call while that tick runs joins it.
- The operator endpoints, `POST /push-tick` and `/v3/admin/sync-policies`,
  live on a separate FastAPI app that the admin listener
  (`TIGERDUCK_ADMIN_PORT`, 9001) serves inside the API process, bound to
  the backend's alias on the internal `tigerduck-admin` network. The API
  port never serves them. Add an operator endpoint there, not to the API.
- The `course` and `assignment` channels deliver to iPhone and iPad only
  (`APPLE_HANDHELD_PLATFORMS`); Android posts its own class reminders.
  The reminder scans file nothing for a user with no such device and
  cancel what they filed. A device whose token is gone for now (APNs
  dropped it, the app has not registered a new one) keeps its filed
  reminders; only new ones wait for a token.
- Before its first delivery, an assignment reminder or `assignmentUrgent`
  start is checked against the user's rows of the assignment (any course
  id, deleted or not) and then Moodle (`push/presend.py`). It fails
  closed: one nobody can confirm still open is not sent, but retried until
  `push_presend_grace_minutes` past its fire time and then cancelled
  (`unverified_before_send`), and a Moodle outage opens a per-tick breaker
  rather than costing a timeout per job.
- An assignment reminder leaves out a device that the assignment's
  `assignmentUrgent` start went to (a sent delivery) from
  `push_countdown_dedupe_window_seconds` (0, off, by default; 300 on) before the
  reminder's fire time on, once the app there registered the activity it
  started (its pending `la_end` job, as `_activity_already_registered`
  reads): the start's alert already rang there. A start iOS cannot show, or
  one the app ends on arrival, never registers. A reminder due within 30 s
  of such a start's send waits once, 60 s, for the registration, when the
  pre-send grace leaves room for the next round; a device still
  unregistered then gets it, as does one whose start did not go out. A
  reminder left out on every device ends `cancelled` (`countdown_start_sent`).
- An iOS alert's `thread-id` comes from its channel (`thread_id_for` in
  `push/payload.py`): `course`, `assignment`, or `other` for the rest.
  Class and homework reminders carry no collapse id: on iOS a shared one
  replaces the notification already on screen, so only one would show.
- An FCM token is `standard` only; Live Activity token kinds are APNs.
- Registering a push token retires the device's other active tokens of
  the same kind (`_retire_superseded_tokens`; scope-narrowed only for
  Live Activity update tokens); a rotated token must not keep its
  predecessor delivering.
- `/sync/full` tags each course tombstone with `deleted_by_reset` and
  `deleted_by_this_device`; a client ignores a tombstone that is both,
  the same rule `upload_courses` applies when it releases them. A course
  an upload's `force_keys` brings to the server is stored as
  `user_added`, unless the key brings back a course deleted while it was
  not; one already there keeps its `source`. A single delete of a
  `user_added` course leaves a tombstone marked `user_added`, which its
  author's snapshot leaves out and its author's next upload forcing no
  key releases. That helps only a removal that hides nothing on the
  device, today the iPhone's tap-again in Add course: the apps' other
  removals keep a local hide, so their rosters leave the course out.
- APNs topic for a Live Activity: `{bundle_id}.push-type.liveactivity`
  (the payload builder handles this — do not hardcode elsewhere)
- Scheduler runs IN-PROCESS in FastAPI's lifespan as a single worker.
  Never spin up a second replica: each would run every job, and every
  push would go out twice.

## ANTI-PATTERNS
- Do not handle Moodle/NTUST credentials outside `server/auth/`.
  v3 keeps the Moodle token (never the NTUST password) encrypted at
  rest via `CredentialCipher` so the server-side sync job can fetch on
  the user's behalf; it is decrypted only in `server/syncjobs/credentials.py`,
  never logged, and dropped with the account. Anything else that needs a
  credential goes through that path.
- Do not sign in to NTUST SSO or Moodle with a student's password. The
  apps sign in themselves and give `POST /v3/auth/login` the Moodle
  token; a login without one answers `400 moodle_token_required`, and
  the `password` the apps still send is ignored.
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
- Do not add sign-in, sessions or an admin list to the portal. It
  stays stateless: `portal/app/access.py` verifies the Cloudflare Access
  JWT (`Cf-Access-Jwt-Assertion`) on every `/api/*` request, the check
  Grafana makes, and only `TIGERDUCK_ENV=development` skips it. Paths
  outside `/api/` are served to anyone, so data goes under `/api/`.
- Do not trust `Cf-Access-Authenticated-User-Email` or any other identity
  header; a client that bypasses Access can set them. The operator is
  `request.state.actor`, taken from the verified token.
- Do not add a portal route that changes state, shows one account's or
  device's data, or lists many students or devices without an `audit()`
  call (`portal/app/audit.py`) inside its transaction;
  `portal/tests/test_audit_coverage.py` fails for one without it. `detail` takes counts and filters, never a student id,
  a typed search, a push body or a token.
- Do not let the portal drive backend lifecycle (start/stop/restart).
  Its docker socket mount is read-only on purpose; container lifecycle
  belongs to the host operator running `./start.sh`. The import flow
  shows the user a "restart manually" banner instead.
