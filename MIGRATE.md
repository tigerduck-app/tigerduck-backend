# MIGRATE.md

How to upgrade the backend to a new version without losing data.

Schema changes are managed by Alembic (`server/migrations/versions/`). Every
migration is forward-only and idempotent against `alembic_version` — running
`alembic upgrade head` against a database that is already current is a no-op.

The backend container's `entrypoint.sh` runs `alembic upgrade head` before
launching uvicorn, so the common case is just: pull, then `./start.sh`
(rebuild, restart).

---

## TL;DR

```bash
# Same host, same DB volume — the normal upgrade.
cd tigerduck-backend
git pull
docker compose pull postgres            # only if Postgres image was bumped
./start.sh                              # rebuilds + restarts the stack; the backend migrates on start
```

`./start.sh` automatically picks the right compose files for the
`TIGERDUCK_ENV` in your `.env`, so the same upgrade flow works for both
development and production hosts without remembering `-f` flags.

The named volume `tigerduck_pgdata` survives `up -d --build` and even
`down` (without `-v`), so data is preserved.

---

## Upgrading to 3.2.0

3.2.0 adds the monitoring stack (Grafana, Prometheus, Loki) and two
migrations. On top of the normal upgrade:

- **Migrations.** `e7b2c9d41f63` adds a partial index on `push_jobs` for
  the stale-lock sweep. `f3c8a1d5b927` moves pending Live Activity jobs
  (channel `schedule`) from priority 100 to 10. Both apply on backend
  start, like every migration.
- **Use `./start.sh`, not a bare `docker compose up`.** `./start.sh`
  writes a `TIGERDUCK_MONITOR_DB_PASSWORD` into `.env` when it is missing.
  Without it the one-shot `monitor-role` service exits with an error, and
  Grafana, `postgres-exporter` and `sql-exporter`, which wait for it, do
  not start.
- **Production: Cloudflare Access.** Set `TIGERDUCK_CF_ACCESS_TEAM_DOMAIN`
  and `TIGERDUCK_CF_ACCESS_AUD` (the AUD tag of the portal's Access
  application) in `.env`. Grafana signs people in from the Access token;
  with these unset, nobody gets in. If the portal host is not
  `portal.tigerduck.app`, also set
  `TIGERDUCK_GRAFANA_ROOT_URL=https://<portal host>/grafana/`.
- **Production: route `/grafana`.** On the portal host, send `/grafana`
  to `tigerduck-grafana:3000`, keeping the `/grafana` prefix. The README's
  Monitoring section has the nginx-proxy-manager and cloudflared steps.

---

## Before any upgrade: take a backup

Even though migrations are reversible in principle, it is cheap insurance
and the only thing that protects you from operator error.

**Preferred path — portal export button.** Visit the portal (dev:
`http://localhost:40010/backup`, prod: through your auth-proxy URL),
click "Download backup." You get a `tigerduck-export-<timestamp>.tar.gz`
containing `pg_dump --format=custom` of the tigerduck DB plus a
manifest. To restore: same page, "Restore" form — then restart the
backend as prompted.

**Manual fallback** — when the portal isn't running (fresh install or
upgrade from a pre-portal commit):

```bash
# From the repo root, while the postgres container is running:
docker compose exec -T postgres \
  pg_dump -U tigerduck -d tigerduck --format=custom --no-owner \
  > "backup-$(date +%Y%m%d-%H%M%S).dump"
```

`--format=custom` is restorable with `pg_restore` and supports parallel
restore. `--no-owner` makes the dump portable across roles. The portal
also accepts this bare `pg_dump` file (no `.tar.gz` wrapper) so you can
import a manual dump through the UI.

To restore that dump into an empty database directly (bypassing the
portal):

```bash
docker compose exec -T postgres \
  pg_restore -U tigerduck -d tigerduck --clean --if-exists --no-owner \
  < backup-YYYYMMDD-HHMMSS.dump
```

---

## Scenario A: in-place upgrade (same host, same DB)

This is what `start.sh` automates. The volume `tigerduck_pgdata` keeps the
data; Alembic walks the version chain from whatever `alembic_version` says
up to `head`.

```bash
cd tigerduck-backend
git pull
./start.sh
```

If you want to apply migrations manually (e.g. to inspect SQL first):

```bash
# Dry-run: print the SQL that would run between current and head.
docker compose exec backend alembic upgrade head --sql

# Actually apply:
docker compose exec backend alembic upgrade head

# Inspect state:
docker compose exec backend alembic current
docker compose exec backend alembic history --verbose
```

---

## Scenario B: move to a new host / new Postgres instance

You have an old DB with real data and want the same data inside a freshly
provisioned Postgres on another machine (or a new volume). **Do not**
`cp -r` the Postgres data directory between different Postgres versions —
use `pg_dump` / `pg_restore`.

1. **Dump from the old host** (while it is still running):

   ```bash
   docker compose exec -T postgres \
     pg_dump -U tigerduck -d tigerduck --format=custom --no-owner \
     > tigerduck.dump
   ```

   The dump includes the `alembic_version` table, so the new database
   will know exactly which migration it is on after restore.

2. **Bring up the new stack with an empty DB**:

   ```bash
   # On the new host
   git clone git@github.com:tigerduck-app/tigerduck-backend.git
   cd tigerduck-backend
   cp .env.example .env   # fill in secrets, POSTGRES_PASSWORD, etc.
   # Use the compose files ./start.sh would (the dev override when
   # TIGERDUCK_ENV=development), but start only postgres for now.
   source ./_compose-files.sh
   docker compose "${COMPOSE_FILE_ARGS[@]}" up -d postgres
   docker compose exec postgres pg_isready -U tigerduck -d tigerduck
   ```

3. **Restore the dump** into the new (empty) `tigerduck` database:

   ```bash
   docker compose exec -T postgres \
     pg_restore -U tigerduck -d tigerduck --no-owner \
     < tigerduck.dump
   ```

4. **Start the stack** with `./start.sh`, not a bare `docker compose up`:
   it writes `TIGERDUCK_MONITOR_DB_PASSWORD` into `.env` (see
   [Upgrading to 3.2.0](#upgrading-to-320)) and starts every service. The
   backend's `entrypoint.sh` runs `alembic upgrade head`. Because the dump
   carried `alembic_version` forward, this either does nothing (you were
   already at head) or applies any migrations newer than the dump:

   ```bash
   ./start.sh
   ./logs.sh              # backend logs
   ```

5. **Verify**:

   ```bash
   docker compose exec backend alembic current
   docker compose exec postgres psql -U tigerduck -d tigerduck -c \
     "select count(*) from device_registrations;"
   ```

---

## Scenario C: imported a dump that has no `alembic_version`

This happens if the dump came from a pre-Alembic snapshot or someone
exported only the data tables. Tell Alembic where the schema currently
stands without re-running migrations:

```bash
# Stamp to a specific revision that matches the schema you imported:
docker compose exec backend alembic stamp <revision_id>

# Or, if the imported schema is fully up to date:
docker compose exec backend alembic stamp head
```

`stamp` only writes to `alembic_version` — it does not run any DDL.
After stamping, `alembic upgrade head` will apply only the migrations
newer than the stamped revision.

---

## Scenario D: rolling back a bad migration

```bash
# Roll back one revision:
docker compose exec backend alembic downgrade -1

# Roll back to a specific revision:
docker compose exec backend alembic downgrade <revision_id>
```

Downgrade only works if the migration's `downgrade()` is implemented
(check the file under `server/migrations/versions/`). For destructive
migrations (column drops, type changes), restore from the `pg_dump` you
took in the "Before any upgrade" step instead.

---

## API version compatibility (clients across an upgrade)

`/v1/*` and `/v2/*` are fully retired. A middleware in `server/main.py`
answers every request to them with **410 Gone**, naming the current API
base path (`/v3`). Clients on an old build must update the app; no
setting brings either version back.

---

## Things that do **not** require a migration

- Bumping `api_v3_base_path` (URL change only).
- Adding a new env var in `config.py` with a default value.
- Code-only changes to dispatcher / scheduler / push routers.

If `alembic upgrade head` reports "no new upgrade operations", you are
already current — no action needed.

---

## Useful commands

```bash
# Show current revision in the live DB:
docker compose exec backend alembic current

# Show full migration history:
docker compose exec backend alembic history --verbose

# Generate a new migration after editing models.py:
docker compose exec backend alembic revision --autogenerate -m "describe change"
# Then review the generated file under server/migrations/versions/ before
# committing — autogenerate is a starting point, not the final answer.
```
