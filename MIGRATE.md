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
git submodule update --init             # app-translation, built into the backend image
docker compose pull postgres            # only if Postgres image was bumped
./start.sh                              # rebuilds + restarts the stack; the backend migrates on start
```

`./start.sh` automatically picks the right compose files for the
`TIGERDUCK_ENV` in your `.env`, so the same upgrade flow works for both
development and production hosts without remembering `-f` flags.

The named volume `tigerduck_pgdata` survives `up -d --build` and even
`down` (without `-v`), so data is preserved.

---

## Upgrading to 3.3.5

- **One row per assignment.** An app's upload of an assignment was stored as
  a placeholder with course id 0 beside the row the server's own Moodle fetch
  keeps under the real course id. A mark made before the fetch first listed
  the assignment went out of `/sync/full` without its Moodle id, so the apps
  dropped it, and the reminder scan read neither that mark nor a submission
  an app reported. The fetch now takes the placeholder over, and once the
  server has its row an upload only reports a submission and a grade there,
  and an initial upload only a submission. Neither takes a submission back,
  and a new one cancels the assignment's pending reminders and countdowns at
  once.
- **Migration `d899fe7d1f79`** merges the placeholders already stored. The
  server's row takes the submission if either row has one, the
  placeholder's grade where it has none, and the placeholder's override;
  where both rows have one, the later mark and the later note win. The
  placeholder is then deleted. A soft-deleted placeholder whose mark
  `/sync/full` has not shown since the server's row came stays as it is, mark
  and note, when the server's row holds no mark or only a clear older than
  that mark: such a mark may be one the user cleared under 3.2.0. Each user
  whose `/sync/full` the merge changes gets one change entry. A moved or
  winning mark or note makes the apps fetch again; a folded submission or a
  copied grade alone only moves their cursor, though an Android build older
  than the one that passes over `assignment` changes runs one full sync.
  Data only; a second run changes nothing.
- **Rolling back.** The 3.3.4 code cannot start on a database at
  `d899fe7d1f79`, so stamp it back to `df3948adb76a` first: with 3.3.5
  running, `docker compose exec backend alembic stamp df3948adb76a`; with
  3.3.4 already built,
  `docker compose run --rm --entrypoint alembic backend stamp --purge df3948adb76a`.
  3.3.4 reads the merged rows as they are, and its uploads add placeholders
  again, which the migration merges when 3.3.5 starts again.
- No app update and no setting.

## Upgrading to 3.3.4

- **One alert per moment for an assignment.** When the "due soon" Live
  Activity's lead time equals an assignment reminder offset (both 8 hours by
  default), the `assignmentUrgent` start and the reminder come due together
  and the iPhone rang twice. With the skip on, the reminder leaves out an
  iPhone that the start went to, from `push_countdown_dedupe_window_seconds`
  before the reminder's fire time on, once the app there registered the
  activity the start created. A reminder due within 30 seconds of such a
  start waits once, 60 seconds, for that registration, when the pre-send
  grace leaves room for it; an iPhone still unregistered then gets the
  reminder, as does one whose start did not go out. A reminder left out on
  every device ends `cancelled` as `countdown_start_sent`. A reminder that
  waited asks Moodle again at its next round, so it costs two pre-send
  checks.
- **The skip ships off** (`push_countdown_dedupe_window_seconds` defaults to
  0), since the start's alert has a title and body and no sound field. Send
  a test countdown from the portal to an iPhone and listen to its alert. If
  it makes a sound, set `TIGERDUCK_PUSH_COUNTDOWN_DEDUPE_WINDOW_SECONDS=300`
  in `.env` and run `./start.sh` (a plain restart does not read `.env`
  again); if not, leave it off.
- **Turning the skip off** again without a rollback: remove that line, or set
  it to 0, and run `./start.sh`.
- New metric: `tigerduck_push_countdown_dedupe_total`, by `outcome`:
  `left_out` (devices), `held` (reminders that waited) and `unregistered`
  (devices that got the reminder after their start went out).
- No migration and no app update.

## Upgrading to 3.3.3

- **Assignment reminders are checked right before they go out.** Before an
  assignment reminder or an `assignmentUrgent` Live Activity start is first
  delivered, the push pipeline checks that the assignment still needs a
  submission: the database first, then one `mod_assign_get_submission_status`
  request with the student's stored token. A submitted assignment cancels the
  job as `submitted_before_send`; one marked done or ignored, in an ignored
  course, gone, past or moved from its due date, closed to submissions, locked
  or extended cancels it as `stale_before_send`. When no answer comes, the job
  is retried every `push_presend_retry_seconds` (60) until
  `push_presend_grace_minutes` (10) after its fire time, then cancelled as
  `unverified_before_send`. Expect about one more Moodle request per reminder;
  once Moodle fails as a whole, a breaker stops asking for the rest of the
  tick.
- New optional settings, shown with their defaults:
  `TIGERDUCK_PUSH_PRESEND_RETRY_SECONDS=60`,
  `TIGERDUCK_PUSH_PRESEND_GRACE_MINUTES=10`,
  `TIGERDUCK_PUSH_PRESEND_TIMEOUT_SECONDS=5`.
- New metric: `tigerduck_push_presend_checks_total`, by `verdict`.
- No migration and no app update.

## Upgrading to 3.3.2

- **The 48-hour reminder sees early submissions.** The submission probe now
  reaches assignments due within 72 hours (`submission_status_window_hours`,
  was 48). At 48 an assignment entered the probe only as its 48-hour
  reminder fired, so a submission made before then was never seen and the
  reminder went out. Each assignment sync now also probes the assignments
  due 48 to 72 hours ahead, one Moodle request each. A deployment that set
  `TIGERDUCK_SUBMISSION_STATUS_WINDOW_HOURS=72` in `.env` as a stopgap can
  remove that line.

## Upgrading to 3.3.1

3.3.1 is a fix release. Sessions survive a lost refresh reply, class
reminders follow the published term, sync jobs keep retrying through a
school outage, reminders and bulletin pushes scale past the database's bind
limit, logins need a Moodle token, and several sync routes answer correctly.
The bullets below say what each change needs at upgrade time.

The session change: an app that sends a rotated refresh token again, while nothing has used the token
that replaced it, gets one more rotation up to 24 hours after the first,
instead of having every session on the device revoked as stolen. Both pairs
work until one of them is used, so a late duplicate of a refresh no longer
ends the session the app keeps; a duplicate that arrives within 60 seconds,
after the app used its pair, is refused. A token revoked for another reason
(logout, device removal, expiry) gets `401 session_revoked` and revokes
nothing else. It has one migration and no new settings to set. On top of
the normal upgrade:

- **Update the submodule.** Run `git submodule update --init` after
  `git pull`. This release pins `app-translation` to a newer commit.
- **Migrations.** `cc54925b7852` adds `first_used_at` and `rerotated_at` to
  `auth_sessions` and allows the revoked reason `superseded`. Postgres adds
  the columns without rewriting the table; the replaced check constraint
  reads every row once under the table lock, about 0.1 seconds per million
  sessions. `bab460b19cc9` adds `local_status_received_at` to
  `user_assignment_overrides` and fills it from `updated_at`, which writes
  every row of that table once. `df3948adb76a` adds `fanout_retry_at` and
  `match_retry_at` to `bulletins`.
- **Sessions from before the upgrade** start with `first_used_at` empty,
  and one in use gets it on its next request. For 24 hours after the
  upgrade, a session the app has not used since may still be replaced when
  the token before it comes back.
- **Monitoring.** `reuse_detected_at` now marks theft revocations only.
  The grace retry no longer sets it, so the "Token reuse" panel and
  `tigerduck_auth_reuse_detected_24h` count fewer; rows from before keep
  theirs.
- **No app update needed.** Both apps treat any 4xx from
  `/v3/auth/refresh` as a lost session, the new `session_revoked` too.
- **Sync answers.** `PATCH /v3/sync/courses/{moodle_id}/override` answers
  a course the server does not have with `404 {"detail": "course_not_found"}`
  instead of `{"detail": "Not Found"}`. A request time the server compares
  with its clock gets 422 instead of a 500 when it has no time zone: the
  override times of the initial upload and the override PUTs, the bulletin
  state times, the schedule sync's `fire_at` and the Live Activity
  register's `countdown_target`. `GET /v3/courses` and
  `PUT /v3/courses/{id}/override` fill each override's `moodle_id`,
  `course_no` and `semester`, and the PUT takes `ignore_assignments`.
  Neither app needs an update: iOS sends these times with a zone and
  Android sends none of them, both count any 404 from the PATCH as a
  refusal, and neither calls the `/v3/courses` routes.
- **Check the semester dates.** Class reminders now go to the courses of
  the term both apps show, the published term that began most recently,
  and they and class Live Activity starts stay quiet on days no published
  term runs on, as both apps already do. A term without dates gets no
  reminders, even once its classes begin. Before upgrading, check that the
  portal's Semester dates page has dates for 1151, and give 1152 its dates
  before its first day of classes. With no term published at all nothing
  changes.
- **Turning the recovery off** without a rollback: set
  `TIGERDUCK_AUTH_REFRESH_RECOVERY_WINDOW_SECONDS=0` in `.env` and restart
  the backend. The 60-second grace retry stays.
- **Sync jobs outlast a school outage.** A Moodle or SSO outage, or a
  Moodle rate limit, that lasts past a job's retries (about 15 minutes with
  the default backoff) no longer leaves the job `failed`: it runs again
  after its policy interval, 8 hours by default, with its attempts reset.
  A job still ends `failed` on our own errors: an unexpected exception, a
  database error while it records submission status, or a worker that
  died during each of its attempts. 3.3.0 runs a job left waiting this way
  at its next run, so a rollback has nothing to undo here.
- **Jobs that failed before the upgrade** stay `failed`. Once the backend
  is up, start them again:

  ```bash
  docker compose exec postgres psql -U tigerduck -d tigerduck -c \
    "UPDATE sync_jobs SET status = 'pending', attempts = 0, run_after = now() WHERE status = 'failed'"
  ```

  The portal's Retry all failed would start them too, but it also starts
  the `disabled` jobs, which fail again at once and can send their users
  another sign-in reminder.
- **Retry buttons.** On the portal's Moodle page, Retry all failed now
  also starts the jobs waiting out an outage: pending jobs whose last run
  failed. Retry all retried only failed and disabled jobs; it now starts
  every Moodle job that is not running.
- **Sync job counts.** An outage no longer raises the number of `failed`
  jobs, on the portal's Moodle page or in "Jobs by status". It shows as
  failed runs ("Failed runs", "Failure reasons"), and the jobs it caught
  stay `pending` with their last error, listed after the failed ones in
  the Jobs table. `tigerduck_sync_oldest_success_age_seconds` now counts
  those jobs, so it keeps climbing through a long outage.
- **TA courses behind a refusal.** When Moodle refused every course a user
  can submit to (courses hidden from their students, for one), the fetch
  kept every stored assignment, so a course the user is a TA in kept its
  assignments from before 3.3.0 and their reminders. Such an answer now
  soft-deletes the assignments of the courses Moodle left out without a
  warning, as any other answer does, and keeps those of the courses it
  refused, and the user's sync log notes how many it deleted. A TA course
  refused along with the rest, as under an unaccepted site policy, keeps
  its assignments until a fetch succeeds. Nothing to do on upgrade or
  rollback.
- **Logins need a Moodle token.** `POST /v3/auth/login` without
  `moodle_token` answers `400 moodle_token_required` and still counts
  against the login limit. A login with a password and no token answered
  500 before, because the code meant to sign in with the password could
  not load. The password the apps send is ignored. No app update is needed.
- **Cleared marks stay cleared.** Under 3.2.0, clearing an assignment's
  done or ignored mark kept a mark made on it before the server's Moodle
  fetch first returned it, which the apps no longer showed. 3.3.0 shows
  that mark again once a fetch soft-deletes the assignment, as it does to
  every assignment of a course the user is a TA in. 3.3.1 does not, and the
  apps drop such a mark on their next full sync; a mark made after that
  first fetch still shows. The rule compares the time each mark reached
  the server, which `bab460b19cc9` records, so editing an assignment's
  note does not bring its cleared mark back. After a rollback 3.3.0 shows
  those marks again.
- **A failing bulletin waits.** A bulletin whose push to devices or match
  to users fails is tried again 15 minutes later instead of on the next
  pass, so the bulletins after it are not held back while it keeps failing.
  Nothing to do on upgrade or rollback.
- **Rolling back.** The 3.3.0 code cannot start on a database at
  `df3948adb76a`: its startup `alembic upgrade head` cannot find that
  revision. Stamp the database back to `ddf188f91a15`, then deploy 3.3.0.
  The new columns stay and 3.3.0 never reads them; a superseded token
  that comes back takes its theft path. Stamp with the image you have:
  - 3.3.1 running: `docker compose exec backend alembic stamp ddf188f91a15`.
  - 3.3.1 restarting in a loop, which `exec` cannot reach:
    `docker compose run --rm --entrypoint alembic backend stamp ddf188f91a15`.
  - 3.3.0 already built:
    `docker compose run --rm --entrypoint alembic backend stamp --purge ddf188f91a15`.
    Without `--purge` it fails with "Can't locate revision identified by
    'df3948adb76a'", since 3.3.0 does not have that migration.

  Returning to 3.3.1 needs no stamp: its migrations skip the columns that
  already exist. Sessions 3.3.0 made in between start with `first_used_at`
  empty, as at the first upgrade, and an override 3.3.0 changed takes its
  `updated_at` as the time its mark reached the server. So does one whose
  note 3.3.1 edited before the rollback, so a cleared mark under it can
  show again, as under 3.3.0.

  `alembic downgrade ddf188f91a15` instead drops the new columns. First it
  writes what 3.3.0 reads in place of the session ones: a token rotated a
  second time gets `reuse_detected_at`, where 3.3.0 marks a grace retry, so
  neither release rotates it again, and a superseded session becomes
  `rotated` with no successor. Upgrading again starts every session without
  `first_used_at`, as the first upgrade did. Stop the backend first
  (`docker compose stop backend`) and run it with
  `docker compose run --rm --entrypoint alembic backend downgrade ddf188f91a15`:
  under a running 3.3.1 every authenticated request answers 500 until
  3.3.0 is deployed.

---

## Upgrading to 3.3.0

3.3.0 lets a user ignore every assignment of a course: the apps send the
setting through the course override PATCH, the full sync returns it, and
the reminder scan skips those assignments. It has one migration and no new
settings. On top of the normal upgrade:

- **Update the submodule.** Run `git submodule update --init` after
  `git pull`. This release pins `app-translation` to a newer commit, and
  the backend image is built from the checkout.
- **Let a backup or restore finish.** This release rebuilds the portal
  image, so `./start.sh` replaces the portal container and stops whatever
  backup download or restore it is running. Start the upgrade once none is.
- **Migration.** `ddf188f91a15` adds `ignore_assignments` (not null,
  default false), `ignore_assignments_updated_at`,
  `ignore_assignments_device_id` and `moodle_course_id` to
  `user_course_overrides`. It applies on backend start. Postgres adds these
  columns without rewriting the table. From this release a migration waits
  at most 10 seconds for a lock: if another transaction holds it longer,
  the backend exits, restarts and tries again.
- **Deploy before the apps.** Ship this backend before any app version
  that ignores courses. Older apps keep working: the new request fields are
  optional and they skip the new response fields.
- **Rolling back.** The 3.2.0 code cannot start on a database at
  `ddf188f91a15`: its startup `alembic upgrade head` cannot find that
  revision. To go back and keep the saved settings, stamp the database
  back to `f3c8a1d5b927`, then deploy 3.2.0. The new columns stay, and
  3.2.0 never reads them. Stamp with the image you have:
  - 3.3.0 running: `docker compose exec backend alembic stamp f3c8a1d5b927`.
  - 3.3.0 restarting in a loop, which `exec` cannot reach:
    `docker compose run --rm --entrypoint alembic backend stamp f3c8a1d5b927`.
  - 3.2.0 already built:
    `docker compose run --rm --entrypoint alembic backend stamp --purge f3c8a1d5b927`.
    Without `--purge` it fails with "Can't locate revision ddf188f91a15",
    since 3.2.0 does not have that migration.

  Returning to 3.3.0 needs no stamp: its migration skips the columns that
  already exist.

  `alembic downgrade f3c8a1d5b927` instead drops the columns and every
  saved ignore setting. Stop the backend first
  (`docker compose stop backend`) and run it with
  `docker compose run --rm --entrypoint alembic backend downgrade f3c8a1d5b927`:
  under a running 3.3.0 every full sync answers 500 and every reminder scan
  fails until 3.2.0 is deployed.

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
git submodule update --init
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
   # The backend image is built with the app-translation submodule.
   git clone --recurse-submodules git@github.com:tigerduck-app/tigerduck-backend.git
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
