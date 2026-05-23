# TigerDuck portal — design

Local-dev / on-prem admin UI for the TigerDuck backend. The portal does
not enforce a signin gate today — front it with **Cloudflare Zero Trust
Application** (or any other auth-proxy) if you need one. The `admins`
table + audit log are still maintained for record-keeping; they are not
load-bearing for access control.

## Goals

- Show backend container + DB + LLM status at a glance.
- Show which env mode the stack is running in (read from `.env`).
- Manage portal admins (bootstrap from `.env`, add/remove via UI).
- Export + import the full stateful surface (postgres + portal DB).
- Toggle the dev-only **Skip LLM probe** behaviour on the backend.
- Custom-push page reserved as a TODO landing pad.

## Non-goals (initially)

- No mode-switching (dev/prod) from the UI — edit `.env`, re-run `./start.sh`.
- No container start/stop/restart from the UI (deferred; uncertain we want
  it given mode switch already requires terminal).
- No real user accounts on the backend itself — "admins" are portal
  admins, not push users.
- No fake-device fanout testing in v1 — folds into the future custom-push
  page if/when it ships.

## Architecture

```
                            ┌────────────────────────────┐
   cloudflare access ──tcp──│ cloudflared (container)    │
                            └────────────┬───────────────┘
                                         │ http
                                         ▼
   ┌───────────────────┐    ┌────────────────────────────┐
   │ docker.sock (RO)  │◄───│ portal (container, :40010) │
   └───────────────────┘    │  FastAPI + Jinja2 + HTMX   │
                            │  state in portal.db (vol)  │
                            └────────────┬───────────────┘
                                         │ HTTP /v2/...
                                         ▼
                            ┌────────────────────────────┐
                            │ backend (:40000)           │
                            └────────────┬───────────────┘
                                         │
                                         ▼
                            ┌────────────────────────────┐
                            │ postgres (tigerduck-db)    │
                            └────────────────────────────┘
```

- Portal joins both `tigerduck-db` (so it can read backend status via the
  same network as the backend would) and `proxy-net` in prod (so
  cloudflared can reach it; the dev override keeps proxy-net out — portal
  is published directly to host port 40010 in dev).
- Portal mounts `/var/run/docker.sock` **read-only** and talks to the
  engine via HTTP-over-UDS (no docker CLI in the image). Scope is
  limited to `GET /containers/{name}/json` — no start/stop/restart in
  v1 (see non-goals).
- Portal mounts the backend's `server/secrets/` read-only too, so the
  status page can show "APNs key present? yes/no" without exposing the
  key itself.

## Data model (portal.db — SQLite)

```sql
-- Admins authorised to use the portal beyond what's seeded by .env.
CREATE TABLE admins (
  email TEXT PRIMARY KEY,
  added_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  added_by TEXT NOT NULL,  -- email of the admin who added this one, or "bootstrap"
  notes TEXT
);

-- Audit log for sensitive actions (admin changes, exports, imports,
-- skip-LLM toggle). Keep forever in v1 — small volume.
CREATE TABLE audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  actor_email TEXT NOT NULL,
  action TEXT NOT NULL,
  detail_json TEXT
);
```

Volume: `tigerduck_portal_data` (own volume; survives
`docker compose down -v` on the backend only because compose down's `-v`
is scoped to the same project — naming with a `portal_` prefix makes
the intent explicit and easy to back up separately).

## Auth model

**No app-level auth.** Every request is allowed regardless of the
`Cf-Access-Authenticated-User-Email` header. The audit log records the
header email when present, otherwise a synthetic `"portal"` actor.

Operators who want a gate front the portal with Cloudflare Zero Trust
(or any other auth-proxy); the same posture applies in dev and prod, so
neither environment surprises the operator. To re-introduce an in-app
gate later: change `require_admin` in `portal/app/auth.py` to look up
the header email in `admins` and 403 on miss — the dependency wiring is
already in place on every route.

Bootstrap admin: `TIGERDUCK_PORTAL_BOOTSTRAP_ADMIN` in `.env` still
seeds the `admins` table on every startup so the record-keeping
convention has a starting point. Removing the bootstrap email is
reversible (the next startup re-adds it).

## Mode display

Portal reads `TIGERDUCK_ENV` from the backend container's environment
(via `docker inspect`) and displays a coloured pill at the top of every
page:

- `development` → green pill, "DEV"
- `production` → red pill, "PROD"

No edit affordance. To switch: edit `.env`, run `./start.sh` from the
terminal.

## Status page (home)

Read-only. One section per concern:

| Section | Source | Shows |
|---|---|---|
| Mode | backend env | `TIGERDUCK_ENV`, `TIGERDUCK_APNS_ENV` |
| Containers | docker inspect | name, state, uptime, restart count |
| Postgres | direct query | connection ok, migration head, row counts (devices, bulletins) |
| LLM | http get | `llm_base_url`, reachable yes/no, `/models` response time |
| APNs | filesystem | key path present, key id, team id |
| FCM | filesystem | credentials path present, project id |
| Skip-LLM toggle | backend env | current value + dev-only toggle |

## Export / import

**Export button** → produces `tigerduck-export-<iso8601>.tar.gz`
containing:

```
tigerduck-export-2026-05-23T19-00-00/
├── postgres.dump      # pg_dump --format=custom of the tigerduck DB
├── portal.db          # SQLite file copy
└── manifest.json      # { version: 1, exported_at, backend_version }
```

No secrets included. Restore on a new machine still needs `.env` +
`server/secrets/` to be brought across by hand (documented gap).

**Import button** → accepts the same `.tar.gz` or just a bare
`postgres.dump` file (for migrating an existing install whose portal
doesn't exist yet). On import:

1. Restore postgres (`pg_restore --clean`) directly into `tigerduck-db`
   over the network — portal already has DB access. Backend keeps
   running (its connection pool will see new schema on next checkout;
   long-running queries error out, which is fine for the import case).
2. Replace portal.db on disk if present (in-process — close the SQLite
   handle, swap the file, reopen).
3. Audit-log the import.
4. **Show a modal**: "Import complete. Restart the backend container
   manually (`./stop.sh && ./start.sh` from `~/tigerduck-backend`) so
   FastAPI re-reads the new DB state cleanly."

Keeping the restart in the user's hands lets the portal stay on a
read-only docker socket — no `docker compose down/up` invocations from
the portal.

**Manifest versioning**: `version: 1` for the bundle format with both
files. Future bundles bump the version; import accepts older versions or
errors with a clear "unsupported version, last supported on tag X."

## Skip-LLM status

Backend gains an env var: `TIGERDUCK_SKIP_LLM_PROBE` (default `false`).
When `true`, `_wait_for_llm` returns immediately and logs `llm.skipped`.

Portal **displays** the current value on the status page as a coloured
pill (same pattern as the dev/prod mode pill):

- `false` → "LLM probe: ENGAGED" (default; matches prod behaviour)
- `true` → "LLM probe: SKIPPED" (dev convenience for fast iteration
  without llama-server running)

No toggle in the UI. To change: edit `.env`, run `./start.sh` from the
terminal. Same model as dev/prod — single source of truth is `.env`,
portal observes only.

## Build sequence

All shipped on a single branch (`feat/web-portal`) by user request,
rather than as separate PRs.

1. ✅ **`start.sh` reads `TIGERDUCK_ENV`** — when `development`,
   auto-loads `docker-compose.dev.yml`. Solved today's port-binding
   issue as a side benefit.
2. ✅ **Portal foundation** — container, FastAPI + Jinja2 templates,
   SQLite on `tigerduck_portal_data` volume, bootstrap admin from
   `.env`, status page (containers / postgres / LLM / secrets).
3. ✅ **Admin management** — list / add / remove + audit log.
4. ✅ **Export / import** — `pg_dump --format=custom` + portal SQLite
   bundled into `tigerduck-export-<timestamp>.tar.gz`. Import accepts
   the same bundle OR a bare `pg_dump` (for migrating an existing
   install). After import, modal asks user to restart backend manually
   so portal stays on a read-only docker socket.
5. ✅ **Skip-LLM status** — backend `_wait_for_llm` env-gated by
   `TIGERDUCK_SKIP_LLM_PROBE`. Portal status page shows a pill
   reflecting current value. No mutation surface in the UI — flip the
   env var in `.env` and restart.
6. ✅ **Custom-push page** — `/custom-push` route + template that says
   "coming soon."

## Open questions to resolve before coding

1. Bootstrap admin format — single email, or comma-separated list?
2. Portal port — 40010 (proposed) or another?
3. Re-introducing an in-app gate — current decision is "no app-level
   auth, front with Cloudflare ZT if needed." Revisit if a deployment
   surfaces where that's insufficient.

## Out of scope (forever or for now)

- Real-user RBAC (everyone is admin or nobody).
- Streaming logs in the browser.
- Editing `.env` from the UI.
- Anything that needs APNs/FCM credentials to leave disk.
