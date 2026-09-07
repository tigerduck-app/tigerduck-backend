# TigerDuck portal — design

Read-only operator UI sitting alongside the TigerDuck backend stack.
Stateless: no SQLite, no admin list, no audit log — every render is a
live pull from docker / postgres / the backend's `/version` endpoint.

The portal does not enforce a signin gate. Front it with **Cloudflare
Zero Trust Application** (or any other auth-proxy) when a gate is
required; the posture is identical in dev and prod.

## Goals

- Show backend / DB / portal container status and the backend version
  at a glance.
- Surface every field `./start.sh` prints (mode, log level, URLs, LAN
  IPs, APNs env, LLM probe state) on a single page so an operator can
  open a tab on their phone via the LAN URL and see everything.
- Stream the last N lines of each container's logs, with topical
  filtering for Android / Apple push activity and a per-tab search.
- Export + import the backend postgres database.
- Custom-push page reserved as a TODO landing pad.

## Non-goals

- No mode-switching (dev/prod) from the UI — edit `.env`, run
  `./start.sh`.
- No container start/stop/restart from the UI. Docker socket mount is
  read-only on purpose; container lifecycle belongs to the host
  operator. The import flow asks the user to restart manually.
- No app-level auth or user accounts. RBAC, sessions, JWT — all out of
  scope. Front with an auth-proxy if needed.
- No SQLite or other portal-local persistent state. If a future feature
  needs it, bring back a volume + a tiny store at that point.
- No streaming logs in the browser. The /logs page is a snapshot of the
  last N lines; reload to refresh.

## Architecture

```
                            ┌────────────────────────────┐
   (optional)         ──tcp──│ auth-proxy (cloudflared,   │
   cloudflare access         │  nginx-auth, …) — out of   │
                            │  scope                     │
                            └────────────┬───────────────┘
                                         │ http
                                         ▼
   ┌───────────────────┐    ┌────────────────────────────┐
   │ docker.sock (RO)  │◄───│ portal (container, :40010) │
   └───────────────────┘    │  FastAPI + Jinja2          │
                            └────────────┬───────────────┘
                                         │ HTTP /version, /health
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

- Portal joins both `tigerduck-db` (so it can directly query postgres
  for the status row counts + `pg_dump` for export) and `proxy-net` in
  prod. The dev override swaps proxy-net for a host bridge so port
  40010 publishes locally.
- Portal mounts `/var/run/docker.sock` **read-only** and talks to the
  engine via HTTP-over-UDS (no docker CLI in the image). Scope is
  limited to `GET /containers/{name}/json` + `GET
  /containers/{name}/logs` — no write ops in v1.
- Portal mounts the backend's `server/secrets/` read-only too, so the
  status page can show "APNs key present? yes/no" without exposing the
  key itself.

## Status page (home)

Single "Overview" table that mirrors `./start.sh`'s status block 1:1:

| Row | Source |
|---|---|
| Mode | `TIGERDUCK_ENV` (env var threaded into the portal container) |
| Log level | `TIGERDUCK_LOG_LEVEL` |
| Backend version | live GET on `http://tigerduck-internal:40000/version` |
| Backend / Portal | `TIGERDUCK_{BACKEND,PORTAL}_PUBLIC_URL` (clickable) |
| APNs env | `TIGERDUCK_APNS_ENV` |
| LLM probe | `TIGERDUCK_SKIP_LLM_PROBE` (skipped / engaged) |
| LLM URL | `TIGERDUCK_LLM_BASE_URL` |
| LAN backend / portal | `TIGERDUCK_HOST_LAN_IPS` (clickable per IP) |

Plus four read-only sections below it: containers (via docker engine
UDS), postgres (alembic head + row counts of core tables), LLM
(`/models` reachability), secrets-on-disk (.p8 / fcm.json presence).

## /logs page

Five tabs (Backend / DB / Portal / Android / Apple), each backed by
`GET /containers/{name}/logs?stdout=1&stderr=1&tail=N&timestamps=1` on
the docker engine UDS. The framed binary stream is demultiplexed
in-process; ANSI escapes are stripped server-side.

Android / Apple tabs are substring-filtered slices of the backend
container's log (needles: `fcm|android|firebase`, `apns|apple|
live-activity|pts`). A per-tab live search and `?tail=N` (up to 5000)
narrow further.

## Export / import

**Export** → `tigerduck-export-<iso8601>.tar.gz` containing:

```
tigerduck-export-2026-05-23T19-00-00/
├── postgres.dump   # pg_dump --format=custom of the tigerduck DB
└── manifest.json   # { version: 1, exported_at, backend_env }
```

No secrets included. Restore on a new machine still needs `.env` +
`server/secrets/` to be brought across by hand.

**Import** → accepts the same `.tar.gz` or a bare `postgres.dump` (for
migrating an existing install from before this portal existed). The
portal runs `pg_restore --clean --if-exists --no-owner` against
postgres over the network and shows a "restart the backend manually"
banner. Keeping the restart in the operator's hands lets the docker
socket stay read-only.

**Manifest versioning**: `version: 1` for the current bundle shape.
Future bumps should accept older versions or error with a clear
"unsupported version, last supported on tag X."

## Skip-LLM probe

Backend has `TIGERDUCK_SKIP_LLM_PROBE` (default `false`). When `true`,
`_wait_for_llm` returns immediately and logs `llm.skipped`. The portal
displays the current value as a pill on the status page — no toggle in
the UI, edit `.env` and run `./start.sh`.

## LAN IPs

`_compose-files.sh::_detect_lan_ips` enumerates `en*` / `eth*` /
`wlan*` interfaces on the host and keeps only RFC1918 addresses. The
result is exported as `TIGERDUCK_HOST_LAN_IPS` and threaded into the
portal container via `docker-compose.yml`'s `environment:` block, so
the status page renders the same IPs the terminal does.
