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

## Upgrading to 3.8.1

- **Class reminders are sent.** The scan now reads the weekday-keyed
  schedule both apps upload, which it took as no meetings before, so it had
  never filed a class reminder. iPhone and iPad accounts with class
  reminders on, the iOS default, get one before each meeting. Accounts with
  only Android devices still get none from the server. The reminders do not
  take the school's course changes into account yet.
- **No migration and no `.env` change.** Rollback: check out 3.8.0 and
  run `./start.sh`. Class reminders already filed then stay pending and go
  out as filed.

## Upgrading to 3.8.0

- **Course changes (#177).** Migration `ea77513de257` adds
  `course_changes`, a course's class changes per term (cancelled, room,
  start, end, note) for one day or a date range, and the SQL functions
  `course_meetings` and `course_class_days`, which resolve a change's
  class days. Operators edit them on the portal's Course changes page. A
  term that still has changes cannot be deleted (409). Nothing outside the
  portal reads the table yet.
- **Stored names.** `users.display_name` holds the name Moodle reports. It
  is written at sign-in, on `PATCH /auth/credentials` and by the course
  sync and submission check, each time after the main work commits, and a
  failed write only logs a warning. The sign-in response's `display_name`,
  always null before, now carries it.
- **Portal redesign (#178).** The portal is rebuilt on a new shell, with
  new routes for users, course changes, the deregister lookup and the
  Moodle student search and timetable. The user list, a user page, the
  lookups and the timetable write audit entries. `/api/version` answers
  503 when the backend cannot be reached.
- **Deregister signs devices out (#176).** It revokes the devices'
  sessions, unlinks their anonymous registrations and drops the stored
  Moodle token once the student has no device left.
- **Custom push to a course.** `POST /api/custom-push` takes `course_code`
  and reaches the devices of the course's students this term, as a popup
  only (422 when it asks to keep a record). A new bulletin's source URL
  must be http or https (422).
- **Class reminder scan (#174)** reads only the terms that run in its
  window.
- **No `.env` change.** Rollback: run
  `docker compose exec backend alembic downgrade 8d70b4ee9cc7`, then
  check out 3.7.0. The downgrade drops `course_changes` and the two
  functions; names already written to `users.display_name` stay.

## Upgrading to 3.7.0

- **TigerSync last-writer-wins (tigerduck-app#689).** Every `/v3` answer
  carries `X-TigerSync-Clock`, the server time in integer microseconds
  since the epoch, and `/v3/sync/full` also sends `server_hlc`. The
  override PATCHes, the holiday PUT, `DELETE /v3/sync/courses/{course_key}`
  and the course upload's `adds` take TigerSync stamps. The later stamp wins per
  field (colour, ignore setting, each locale of a course name, assignment
  mark, holiday setting) and per course key (add, hide, removal, term
  reset, full reset), and a tie keeps the stored value. A stamp more than
  5 s ahead of the server is refused with 409 `clock_ahead` before anything
  is written. A request with `X-TigerSync-Protocol: 2` must send its stamps
  (422 `hlc_required`). Without the header, a direct edit with no stamp
  counts at the time it arrives, so released apps keep today's behaviour
  and need no update. A value in a bulk upload, which has no stamp (the
  initial upload, the roster's colour fill), counts as the oldest and only
  fills a value that is not set.
- **Migration `88dbb93dba99`** adds `user_course_overrides.custom_names_updated_at`,
  one stamp per course-name locale, and fills each stored value that has
  no stamp with its row's `updated_at`. The column is added without
  rewriting the table.
- **Migration `8d70b4ee9cc7`** adds `user_courses.added_at`, set to
  `created_at` for courses added by hand, and `user_semester_resets`, one
  row per user and term holding the latest reset, filled from the reset
  tombstones.
- **Sync jobs wait for a device in use.** A user's Moodle jobs run only
  while a device with TigerSync on was seen within
  `TIGERDUCK_SYNC_JOB_DEVICE_ACTIVE_DAYS` (default 90) or took an accepted
  standard push within `TIGERDUCK_SYNC_JOB_DEVICE_PUSH_DAYS` (default 14).
  The other users' jobs stay `pending`; a sign-in, launch, full sync,
  upload or run-now moves the device and runs any job already due.
- **Daily device retirement.** The `device_retirement` job runs at 04:00
  Asia/Taipei and deregisters, as the portal does, every device with no
  live session that for 7 days was not seen, did not announce itself and
  took no accepted standard push. Signing in on the device brings it back.
  The first run after the upgrade retires every device that already
  qualifies.
- **No `.env` change.** Rollback: `alembic downgrade 5d2a9c7e4f1b`, then
  check out 3.6.0. The downgrade drops the new column and table and the
  removal tombstones kept beside a school-listed course.

## Upgrading to 3.6.0

- **The course catalog (tigerduck-app#547).** `GET
  /v3/course-catalog/{semester}?language=zh|en` (JWT) serves a term's whole
  course list with its colleges, departments and general-education
  dimensions, for the app's on-device course search. It is sent gzip
  compressed (plain to a client that does not accept gzip) with a weak
  `ETag` and `Cache-Control: private, no-cache`, answers 304 to a matching
  `If-None-Match`, and answers 404 `course_catalog_not_found` until a
  refresh has stored the term. The ETag hashes the content without the
  body's `fetched_at`, so a refresh that finds the same catalog keeps the
  snapshot as it is, `fetched_at` included, and a device that holds it gets
  a 304 on later days too.
- **Migration `5d2a9c7e4f1b`** adds `course_catalog_snapshots`, one row per
  term and language holding the gzip-compressed JSON served, and
  `course_catalog_en_texts`, one row per term holding the en course names,
  teachers and notes of the day's en sweep.
- **The refresh.** The `course_catalog_refresh` job runs every
  `TIGERDUCK_COURSE_CATALOG_REFRESH_INTERVAL_MINUTES` (default 120; 0 turns
  it off), the first time one interval after the backend starts, so a
  restart puts the next run up to an interval out. It fetches the current
  term from the academic calendar and every later term
  `querycourse.ntust.edu.tw` lists, three at most. Each run sweeps zh as 281
  course-number prefix queries, three in flight at most, and sends one
  `OnlyIntensive` query and the option lists. A zh run takes about a minute
  and some 200 s of the school's time per term, a run with en twice that.
  Until the first run has stored a term, the app keeps querying the school
  itself.
- **The en text.** en is swept too only on the first run after 05:00 Taipei
  time each day, or when a term has no en text stored. The runs between
  build the en snapshot from their zh rows and the stored en text: a new
  course shows its zh text until the next en sweep, and a course already
  there, with the same course number and periods, keeps its stored en
  name, teacher and notes until then, even when zh has changed them.
- **Failures.** A request is retried twice with backoff; a term with any
  failed zh request keeps its last snapshot, and the job reports an error.
  So does a term whose zh courses drop by more than 10% from its stored
  snapshot or lose every intensive course, since the school can answer a
  query with nothing in a hiccup; a term's first snapshot has nothing to
  compare with and is stored. An en sweep that fails, or that has under 90%
  of the run's zh course numbers, still lets zh be stored, with en built
  from the stored text; en stays due, so the next run sweeps it again, and
  the job reports an error. A term listed after the
  current one with no course yet is skipped with a log line and is not an
  error.
- **Alert.** Grafana's new rule `Course catalog not refreshed` posts when
  the job has not completed for over 6.5 hours, three missed runs, while it
  is scheduled. Grafana reads its provisioning only at start, so after
  `./start.sh` run `docker compose restart grafana`.
- No change to `.env` is needed.
- **Rollback.** `alembic downgrade 8996a3bf992f` drops both tables; the
  previous release neither reads nor writes them.

## Upgrading to 3.5.1

- **Link checks complete a chain that leaves out its intermediate
  (#156).** A host such as `www.academic.ntust.edu.tw` sends only its leaf
  certificate, so 3.5.0 failed `ntust-calendar` on every check. The checker
  now fetches the missing intermediates from the leaf's AIA CA Issuers URL,
  as the TLS pin watcher already did, and verifies the chain against the
  certifi roots; a fetched intermediate never becomes a trust anchor. The
  AIA fetch connects only to public addresses and ends with the check's
  deadline.
- No migration and no app update. The next check of `ntust-calendar`
  after the restart passes and sends the recovery alert.

## Upgrading to 3.5.0

- **Links at `/links/<name>` (#108).** `GET
  https://api.tigerduck.app/links/<name>` answers `307` to the link's
  target with `Cache-Control: no-store`, without auth and outside `/v3`; an
  unknown or disabled name, or a target off the allowed hosts, answers
  `404`. nginx-proxy-manager already forwards the path; nothing changes
  there.
- **Migration `35df7613737a`** adds `links` and seeds `ntust-calendar`,
  which goes to `https://www.academic.ntust.edu.tw/p/404-1048-78935.php`
  and is checked for status 200, the final host
  `www.academic.ntust.edu.tw` and at least one `.ics` link. A row already
  there is kept.
- **Allowed hosts.** A target must be `https` on a host in
  `TIGERDUCK_LINKS_ALLOWED_HOSTS` (a JSON list, default `["ntust.edu.tw"]`,
  each with its subdomains), on the default port, with no user name,
  password, whitespace or control character. It is checked when saved, when
  served, before each check and on every redirect the checker follows.
- **The portal's Links page** lists the links and creates, edits, enables,
  disables and tests them. It goes through new routes on the admin listener
  (`/admin/links` on `:9001`), which record `link.create`, `link.update`,
  `link.enable`, `link.disable` and `link.test` in `admin_audit_log`.
  Rebuild both with `./start.sh`.
- **The link checker.** The `link_check` job (every minute,
  `TIGERDUCK_LINK_CHECK_TICK_SECONDS`, 0 turns it off) fetches each enabled
  link at its interval (60 minutes unless set) from the backend container,
  with that container's trust store, and runs its rules. The first check of
  `ntust-calendar` runs right after the deploy; press Test on the Links
  page to see it. A certificate error there means the container cannot
  validate the school's chain, and the alert below fires until it can.
- **Alerts.** Grafana's new rule `Link check failing` posts to Discord once
  when a link fails, naming the rule it failed and what was found in the
  check that started the failure, and again when it passes or is disabled.
  That detail and the alert state are kept on the row, and the rule reads
  the last value of the past 15 minutes, so a restart or an outage of the
  backend shorter than that neither repeats nor clears the alert; a longer
  one clears it and the next start posts it again. Grafana reads its
  provisioning only at start, so after `./start.sh` run
  `docker compose restart grafana`.
- **The apps switch later.** They keep following
  `https://r.xinshou.tw/ntust-calender` until their releases move to
  `/links/ntust-calendar` (tigerduck-app#687, tigerduck-app-android#375);
  keep `r.xinshou.tw` serving until old versions fade.

- **The TLS pin document.** `GET /.well-known/tigerduck/tls-pins.json`,
  public and outside `/v3`, serves the signed pin document the apps take
  their TLS pins from, with `Cache-Control: public, max-age=300` and an
  `ETag`, answering 304 to a matching `If-None-Match`. It answers 404 until
  the first document is published. Migration `8996a3bf992f`, which follows
  the links migration, adds `tls_pin_sets` and `tls_pin_documents` and seeds
  the `ntust` and `library` sets with the apps' built-in pins, expiries and
  monitored hosts, all with `auto_publish` off.

- **Move the signing key before starting 3.5.0.** The key now lives in its
  own folder, `server/signing/`, which `docker-compose.yml` mounts read-only
  into the backend only. The portal mounts `server/secrets` and has the
  Docker socket, so the key must not stay there. On the host, before
  `./start.sh`:

  ```bash
  mkdir -m 700 server/signing
  mv server/secrets/tls-pins-*.pem server/signing/
  ls -l server/signing    # the key stays -rw------- (0600)
  ```

- **The signing key.** The document is signed with the Ed25519 key
  `server/signing/tls-pins-td-pins-2026a.pem` (PKCS #8 PEM, mode 0600),
  which is the default of the new `TIGERDUCK_TLS_PIN_SIGNING_KEY_PATH`, so
  `.env` needs nothing. The key_id the apps know it by, `td-pins-2026a`, is
  the file name between `tls-pins-` and `.pem`. Without the file the backend
  starts, logs `tls_pins.signing_disabled` and publishes nothing; the apps
  keep their built-in pins. To make a new key, on the host (the container
  mounts `server/signing` read-only):

  ```bash
  uv run python scripts/generate_tls_pin_key.py <key_id>
  # or, the same format:
  openssl genpkey -algorithm ed25519 -out server/signing/tls-pins-<key_id>.pem
  chmod 600 server/signing/tls-pins-<key_id>.pem
  ```

  The script writes into `server/signing`, refuses to overwrite a file and
  prints only the key_id and the raw public key in base64. The standby key
  `td-pins-2026b` stays offline. To rotate, first ship apps that embed the
  new public key, then point `TIGERDUCK_TLS_PIN_SIGNING_KEY_PATH` at the new
  file and `./start.sh`.

- **The first document.** With the key in place the backend publishes the
  first document as it starts (reason `initial`). Its serial is the apps'
  `baseline_serial`: it is the first row under Documents on the portal's new
  TLS pins page, and in `docker compose logs backend | grep tls_pins.published`.
  A document is valid for 30 days and is signed again under a new serial
  when 7 days are left, by an hourly job that runs even with the watcher
  off.

- **The pin watcher.** Every hour, starting a minute after the backend
  starts, it opens TLS to each monitored host (ports 443, 993 and 465 on the
  NTUST hosts) with the container's trust store, completing a chain NTUST
  sends short through its AIA address over HTTP. A chain that shares no pin
  with its set gets its intermediates and root added to the set; with
  `auto_publish` off they are marked "not published" and left out of every
  document, whatever else publishes, until an operator presses Publish now,
  and the chain mismatch alert keeps firing until then. The portal saves, and
  the watcher connects to, only host names under their set's domains, on
  ports 443, 465 or 993, that resolve to public addresses.
  `TIGERDUCK_TLS_PIN_WATCH_INTERVAL_SECONDS` (3600; 0 turns it off) and
  `TIGERDUCK_TLS_PIN_WATCH_TIMEOUT_SECONDS` (30, per host) tune it.
  The portal's TLS pins page shows each pin's first and last seen, each
  host's last result and chain, and the document history, and carries the
  pin actions; each one is recorded in `admin_audit_log`.

- **TLS pin alerts.** Six rules in `monitoring/grafana/provisioning/alerting/rules.yml`
  go to the Discord channel: the watcher not reaching a host for 3 hours, a
  chain failing system trust, a chain sharing no pin with the served
  document, a published change to a set, the document within 3 days of
  expiry, and a set within 60 days of expiry. A route in `policies.yml`
  repeats the set expiry alert weekly instead of every 4 hours. Grafana reads
  both files only at start: `docker compose restart grafana` after
  `./start.sh`.

- **Turning on `auto_publish`.** Leave it off until the watcher has run for
  a while without alerts you did not expect. First check that the
  container's trust store validates every monitored host's chain, TWCA
  CYBER Root CA included:

  ```bash
  docker compose exec backend sh -c 'ls /etc/ssl/certs | grep -i twca'
  docker compose exec backend python -c "from server.tls_pins.watcher import probe; r = probe('www.ntust.edu.tw'); print(r.status, r.detail, [c.subject.rfc4514_string() for c in r.chain])"
  ```

  and that every host on the portal's TLS pins page shows `trusted`. Then
  tick Auto-publish for each set there.

- **Rolling back.** Before any app uses the link, run
  `docker compose exec backend alembic downgrade c5e8a2f71d40` first; it
  drops `links` and both TLS pin tables. After an app release uses the link,
  a rollback leaves that release without the calendar. To roll back only the
  TLS pins, downgrade to `35df7613737a` instead. The apps keep the last pin
  document they verified until it expires, then use their built-in pins.

---

## Upgrading to 3.4.1

- **Assignment reminders count from the student's own due date.** An
  assignment's `due_at` is now the date that applies to the student: the
  later of Moodle's due date and the student's extension. An extension
  past the cutoff moves `cutoff_at` to it too. Moodle's list already
  applies a user or group override; the extension comes from the
  submission probe (`mod_assign_get_submission_status`), the call that
  already checks for a submission, so there is no new kind of request.
  The iPhone app from 2.6.5 uploads the same date for its placeholders,
  and a fetch that takes one over keeps it.
- **The probe asks about more assignments.** Besides those due in the
  next `TIGERDUCK_SUBMISSION_STATUS_WINDOW_HOURS` (72), it now asks about
  those due in the past 72 hours and not submitted, since an extension
  granted after the due date moves it forward, and about every one with an
  extension, so a withdrawn one moves the date back. It also asks, whatever
  the due date, about each one whose stored date the sync just moved back to
  Moodle's (an app's upload of the extended date, or a teacher moving the
  date earlier): an extension in the answer moves it forward again. Each is
  one request per assignments sync, as before.
- **A long reminder can miss an early extension.** The server learns of an
  extension only once its assignment is within 72 hours of the due date,
  unless an app uploaded the extended date, so a reminder offset longer
  than that (3 or 7 days) is still filed against Moodle's date.
- **A probe that fails keeps the dates.** An assignment the probe gets no
  answer for keeps its last known extension, as does one whose answer gives
  the extension in a shape that cannot be read. An answer without one moves
  the dates back to Moodle's.
- **The pre-send check follows.** A reminder or countdown at the
  student's extension goes out; one at an extension Moodle no longer
  gives, or shortened, is cancelled as `stale_before_send` (reason
  `extension_withdrawn`); its reminders are filed again once the next
  sync stores the new date. One whose student now has a later extension
  is cancelled as before (`extension`).
- **Migration `c5e8a2f71d40`** adds `extension_due_at` (nullable) to
  `user_assignments` without rewriting the table. Existing rows start
  without an extension; the first probe of each fills it in, so an
  extension already granted shows up within one sync interval (8 h) of
  its assignment entering the probe window.
- **A send that raises is retried alone.** An APNs connection error used
  to roll back a job's whole delivery round, so its next round pushed again
  to the devices it had already reached. The delivery is now recorded as
  failed (`failure_code` `exception`, log event `push.send_raised`) and
  retried in the next round like any other transient failure.
- **A re-synced Live Activity start takes its new time.** `/schedule/sync`
  moves a pending start to the fire time and snapshot the device posts
  again (a changed lead time, a moved deadline, a renamed course), and
  cancels one moved into the past. Before, the old ones stayed.
- **A countdown start aimed at a moved deadline is cancelled.** The
  pre-send check cancels an `assignmentUrgent` start whose
  `countdownTarget` no row of the assignment is due at, as
  `stale_before_send` (reason `due_changed`); the device's next sync files
  it for the new date.
- **Operator announcements stay.** The bulletin stale sweep marks only
  scraped bulletins (`source = 'ntust_general'`), so portal announcements
  (`manual`) and kept custom pushes (`custom_push`) no longer disappear
  about 30 minutes after posting. Ones already hidden stay hidden; the
  upgrade does not bring them back.
- **Class reminders are titled in each device's language.** The title is
  `notification_class_preparing_title` ("即將上課：…", "Class starting soon:
  …") and the body the class's start time and room ("10:20 · TR-212"), in
  place of the Chinese-only "上課提醒：…" and "10 分鐘後上課 · …".
  Reminders filed before the upgrade, up to 48 hours ahead, go out with the
  old text. None of these five changes needs a migration.
- **Rolling back.** With 3.4.1 running, run
  `docker compose exec backend alembic downgrade d07bc39dde6c` first; it
  drops the column. 3.4.0 then writes Moodle's dates back on its next
  sync of each account.
- **A client's sync no longer holds back the server's (#24).** `GET
  /v3/sync/full` leaves the server's Moodle sync where it is. An
  assignment upload holds back only the assignment sync, and a course
  upload only the course sync, by 2 hours as before, but never moves a run
  earlier and never holds back a job a policy interval (8 h) past its last
  success. An app uploading every 15 minutes, as Android does, delays the
  server's sync by at most 2 hours past its schedule.
- **No server fetch for a student no device syncs (#85).** The worker
  claims a job only while the student has a device with TigerSync on.
  Until then the job stays `pending` and is left alone, and it runs at the
  next tick once a device signs in or turns sync on. Signing out of the
  last device (the apps delete it) also deletes the stored Moodle token;
  the next sign-in stores a new one. The portal's deregister keeps it: it
  leaves the device's sessions live, and the app's next request brings the
  device back.
  The overdue metric and the Moodle sync dashboard's Overdue panel leave
  such jobs out: restart `sql-exporter` after `./start.sh`, which reads
  its collectors only at start.
- **Suspend sync stops the fetch (#86).** While the portal's suspension
  runs, the sync tick claims nothing, and a tick the suspension lands in
  hands back the jobs it has not started (their runs `cancelled`, error
  `sync_suspended`). The Moodle maintenance window from `.env` still only
  reschedules a run that failed inside it.
- **Assignment sync off keeps the assignment list (#30).** With
  Assignments off on every device, only the marks (done, ignored) are
  deleted; the list goes too only once assignment reminders are off on
  every device as well.
- **Retry all's expire notification (#54)** goes only to the students
  with a `disabled` job, the ones waiting for a sign-in, and an iPhone or
  iPad syncing, rather than to every student with a healthy job.
- These five need no migration; rolling back to 3.4.0 brings the old
  behavior back. A token deleted at sign-out stays deleted.

---

## Upgrading to 3.4.0

- **The admin endpoints moved to an internal port.** `POST /push-tick` and
  `GET/PATCH /v3/admin/sync-policies` are no longer served on `:40000`;
  they listen on `:9001` (`TIGERDUCK_ADMIN_PORT`), bound to the backend's
  alias `tigerduck-internal-admin` on a new internal network,
  `tigerduck-admin`, that only the portal joins. They still check
  `X-Push-Token`. `docker-compose.yml` adds the network and sets
  `TIGERDUCK_ADMIN_HOST`, and the portal now calls
  `http://tigerduck-internal-admin:9001`, so rebuild both with `./start.sh`;
  nothing changes in nginx-proxy-manager. Anything else that called these
  paths through `api.tigerduck.app` must call them from inside the stack
  instead, for example:

  ```bash
  docker compose exec backend python -c "import httpx, os; print(httpx.get('http://tigerduck-internal-admin:9001/v3/admin/sync-policies', headers={'X-Push-Token': os.environ['TIGERDUCK_API_SHARED_SECRET']}).text)"
  ```

- **An unset `TIGERDUCK_ENV` now means production** for the backend and the
  portal, as it already did for `./start.sh`. A production backend refuses
  to start without `TIGERDUCK_API_SHARED_SECRET`. A development `.env`
  must say `TIGERDUCK_ENV=development` (`.env.example` does).

- **The portal checks the Cloudflare Access token.** Outside
  `TIGERDUCK_ENV=development`, every `/api/*` request to the portal needs a
  `Cf-Access-Jwt-Assertion` that verifies against
  `TIGERDUCK_CF_ACCESS_TEAM_DOMAIN` and `TIGERDUCK_CF_ACCESS_AUD`, the two
  values Grafana already uses; `docker-compose.yml` now passes them to the
  portal. With either unset, or `TIGERDUCK_ENV` missing from `.env` (now
  read as production), the portal logs an error at start and answers 403
  to every API request. A request that skips Access (LAN straight to NPM,
  another container on `proxy-net`) gets 403 too.

- **Migration `d07bc39dde6c`** adds `admin_audit_log`, where the portal
  records each change it makes, each look at one student's data and each
  list of students or devices. It
  applies on backend start. Rows older than
  `TIGERDUCK_ADMIN_AUDIT_RETENTION_DAYS` (default 365) are deleted daily.

- **After deploying**, open the portal through Access: the status page
  should load and show your email at the top. Then open
  `https://<portal host>/api/audit` in the same browser; it lists the
  actions recorded so far, with your email as the actor. A 403 there means
  the two Access values are unset or do not match the portal's Access
  application.

- **Off-site backups run nightly from launchd.** A launchd job on the host
  (`deploy/launchd/ai.tigerduck.backup.plist`) runs
  `deploy/backup/tigerduck-backup.sh nightly` at 04:30: `pg_dump -Fc` of the
  compose Postgres, encrypted with gpg (AES-256) on its way to a Cloudflare
  R2 bucket under `postgres/`, and backups there older than 30 days deleted.
  On Sundays it then restores the newest backup into a throwaway
  `postgres:17-alpine` container with no network and logs the alembic
  revision and row counts of the core tables. Each outcome is written to
  `ops.backup_status` (created by the `monitor-role` service on every
  `./start.sh`), which sql-exporter exports for the alerts. The host needs
  `rclone`, `gpg` and `docker`, all from Homebrew.

  1. In Cloudflare R2, create a bucket and an API token with Object Read &
     Write on that bucket only. Add to `.env`:

     ```bash
     TIGERDUCK_BACKUP_S3_ENDPOINT=https://<account id>.r2.cloudflarestorage.com
     TIGERDUCK_BACKUP_S3_BUCKET=<bucket>
     TIGERDUCK_BACKUP_S3_ACCESS_KEY_ID=<token access key id>
     TIGERDUCK_BACKUP_S3_SECRET_ACCESS_KEY=<token secret access key>
     TIGERDUCK_BACKUP_PASSPHRASE=<python -c "import secrets; print(secrets.token_urlsafe(32))">
     ```

     Keep the passphrase in the password manager, next to `.env`'s
     `TIGERDUCK_CREDENTIAL_KEYS` and `TIGERDUCK_AUTH_REFRESH_HMAC_KEY` and the
     files in `server/secrets/`. Without the passphrase no backup can be
     read; without the others a restored database has Moodle tokens nothing
     can decrypt, refresh tokens that no longer verify and no APNs key.
  2. Pull and `./start.sh`, restart `sql-exporter` (it reads its collectors
     only at start, and `./start.sh` leaves an unchanged container running),
     then run a backup and a drill by hand and install the job:

     ```bash
     export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
     docker compose restart sql-exporter
     deploy/backup/tigerduck-backup.sh backup
     deploy/backup/tigerduck-backup.sh drill
     ln -sf "$(pwd)/deploy/launchd/ai.tigerduck.backup.plist" ~/Library/LaunchAgents/
     launchctl bootstrap gui/$UID ~/Library/LaunchAgents/ai.tigerduck.backup.plist
     ```

     The plist points at the checkout in
     `/Users/xinshoutw/selfhost/Docker/tigerduck-backend`; edit it if yours
     is elsewhere. Its log is `~/Library/Logs/tigerduck-backup.log`.
  3. Check: `deploy/backup/tigerduck-backup.sh list` names the new backup,
     and `docker compose exec -T postgres psql -U tigerduck -d tigerduck -c 'TABLE ops.backup_status'`
     has a `backup` and a `drill` row, and Prometheus has
     `tigerduck_backup_last_success_timestamp_seconds` for both kinds. `launchctl kickstart
     gui/$UID/ai.tigerduck.backup` runs the job now. To remove it:
     `launchctl bootout gui/$UID ~/Library/LaunchAgents/ai.tigerduck.backup.plist`,
     then delete the symlink.

- **Restore from R2 by hand.** List the backups (the names sort by UTC
  time) and download one, decrypted, into the current directory:

  ```bash
  deploy/backup/tigerduck-backup.sh list
  deploy/backup/tigerduck-backup.sh fetch                                # the newest
  deploy/backup/tigerduck-backup.sh fetch tigerduck-20261101T203000Z.dump.gpg
  ```

  That writes `tigerduck-<UTC>.dump`, a plain `pg_dump` file. Restore it
  with the portal's Restore button, or with the backend stopped:

  ```bash
  docker compose stop backend
  docker compose exec -T postgres pg_restore -U tigerduck -d tigerduck \
    --clean --if-exists --no-owner --single-transaction < tigerduck-<UTC>.dump
  ./start.sh
  ```

  Then delete the `.dump` file: it holds every account's data unencrypted.

- **The portal's Restore keeps a copy.** Before it restores an upload, the
  portal dumps the live database to `pre-restore-<UTC>-<suffix>.dump` in
  the new `tigerduck_portal_pre_restore` volume, and restores nothing if
  that dump fails; a second restore started meanwhile is refused. To get
  one back, list them with
  `docker compose exec portal ls /var/lib/tigerduck-portal/pre-restore`,
  then `docker cp tigerduck-portal:/var/lib/tigerduck-portal/pre-restore/<name> .`
  The volume keeps the newest five. When `pg_restore` itself fails, the
  error names the dump taken just before.

- **Alerts go to Discord.** Grafana now provisions alert rules
  (`monitoring/grafana/provisioning/alerting/rules.yml`) and sends every
  alert to one Discord channel. In that channel, create a webhook (Server
  Settings → Integrations → Webhooks) and add its URL to `.env`, then
  `./start.sh`:

  ```bash
  TIGERDUCK_ALERT_DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/<id>/<token>
  ```

  Without it Grafana still starts and evaluates the rules, but every
  notification fails (its log says `Notify for alerts failed`). To check
  delivery: Grafana → Alerting → Contact points → `tigerduck-oncall` → Test.
  The backup rules read `tigerduck_backup_last_success_timestamp_seconds`
  and `tigerduck_backup_last_failure_timestamp_seconds` from the off-site
  backup job; on a host without that job the staleness rules fire half an
  hour after the deploy.

- **Readiness checks.** The backend and the portal answer `GET /ready` with
  200 when they reach Postgres (the backend also needs its scheduler
  running) and 503 otherwise, and compose health-checks both, so `docker ps`
  and the portal's Status page show `unhealthy` when one is wedged.
  `/health` is unchanged.

- **Database timeouts.** Every backend connection now carries
  `statement_timeout` (`TIGERDUCK_DATABASE_STATEMENT_TIMEOUT_SECONDS`, 30)
  and `idle_in_transaction_session_timeout`
  (`TIGERDUCK_DATABASE_IDLE_IN_TRANSACTION_TIMEOUT_SECONDS`, 60); 0 turns
  one off. The push sends and the retention purges lift both for their own
  transactions. Migrations use their own connection and are not affected.

- **A second connection pool.** Scheduled jobs and the sync and push
  workers use a pool of their own (5 + 5 overflow, like the API's), so the
  backend may hold up to 20 Postgres connections instead of 10. The
  `tigerduck_db_pool_*` metrics still describe the API's pool only.

---

## Upgrading to 3.3.6

- **A course added by hand and removed can come back with the roster.**
  Removing a course added by hand left the tombstone that hiding an
  enrolled course leaves, so once the student really took the course, both
  apps kept it hidden until it was added by hand again. A course an
  upload's `force_keys` brings to the server is now stored as `user_added`,
  unless the key brings back a course deleted while it was not; a course
  the server holds already keeps its `source`. Deleting a `user_added`
  course, by hand or with a semester reset, leaves a tombstone marked
  `user_added`. The device that deleted it by hand does not get that
  tombstone in `/sync/full`, and the first upload from it with no
  `force_keys` that lists the course releases it. Every other device stays
  bound, and hiding an enrolled course binds every device as before.
- **Only a removal that hides nothing on the device benefits**, which today
  is the iPhone's tap-again in Add course (`removeUserAddedCourse`).
  Android's delete and the iPhone's and Mac's timetable Delete keep a hide
  on the device, so that device's roster uploads leave the course out, and
  the course stays hidden as before until the apps let go of that hide.
- **When the release moves.** Another account signing in on the same
  install deletes the first account's device row, so the tombstone loses
  its author and binds every device again. Another device's semester reset
  turns the tombstone into its own reset tombstone, which only that device
  releases. An upload the removing device put together before the removal
  and sent after it brings the course back: that window is one request.
- **A cleared course name reaches the other devices.** Clearing a course's
  name in one language used to drop that language from `custom_names`, so
  `/sync/full` no longer listed it and every other device kept the old name;
  clearing the last one left no names at all, which the iPhone skipped. The
  cleared language now stays as `""`, which the override PATCH answer and
  `/sync/full` send, and `PUT /v3/courses/{id}/override` and the initial
  upload clear a name the same way. Clearing a language that never had a
  name stores `""` too, which the other devices apply as a clear that
  changes nothing. The iPhone already reads `""` as cleared; an Android
  build that does not keeps the old name, as before. Class reminders and
  the portal's data panel read `""` as no custom name. A name cleared before
  the upgrade is not sent again: a device that kept the old name keeps it
  until the name is cleared once more.
- **Migration `a463951f49a7`** adds `user_added` (not null, default false)
  to `user_course_tombstones` without rewriting the table. Nothing recorded
  which courses were added by hand before this release, so there is no
  backfill: a course with a tombstone from before 3.3.6 keeps today's
  behaviour, also once it is added by hand again and removed.
- **Rolling back.** The 3.3.5 code cannot start on a database at
  `a463951f49a7`, so stamp it back to `d899fe7d1f79` first: with 3.3.6
  running, `docker compose exec backend alembic stamp d899fe7d1f79`; with
  3.3.5 already built,
  `docker compose run --rm --entrypoint alembic backend stamp --purge d899fe7d1f79`.
  The column stays and 3.3.5 never reads it: every tombstone binds and goes
  to every device again, and a device that syncs meanwhile hides the course
  it removed for good, as before.
- **A refresh token that comes back no longer signs the device out.** A
  rotated token sent again is rotated again each time while nothing has
  used a session it was rotated into, until the token itself expires (90
  days), instead of once within 24 hours. The pairs it gets stay live until
  one of them is used, the 16 newest at most: a send that would leave a
  seventeenth supersedes the oldest, so a copy or a broken client that
  keeps sending the token does not keep adding live sessions. From then on
  the token and the other pairs get `401 session_revoked`, and nothing is
  revoked: the session the app uses keeps working. Such a reuse used to
  revoke the chain and every session of the device, which signed out
  phones that had only lost replies, some for good.
- **A copied refresh token is no longer contained.** A copy sent before the
  app uses its pair gets a pair of its own; whichever pair is used first
  keeps working until it expires, and the other is refused. Signing out in
  the app removes the device, which still ends every session on it, now
  including one a refresh in flight adds while the removal runs.
- **Monitoring.** A reuse refused past the 60-second grace still logs the
  warning `auth.refresh.reuse_detected`, now with a `cause`
  (`successor_used`, `superseded` or `successor_revoked`) and
  `seconds_since_rotation`, and still sets `reuse_detected_at`, which the
  "Token reuse" panel and `tigerduck_auth_reuse_detected_24h` count. One
  refused within the grace is logged at info as
  `auth.refresh.duplicate_refused`, as before. `auth.session.superseded` is
  one line per use, with every session it superseded in `session_ids`;
  `auth.refresh.grace_retry` and `auth.refresh.lost_rotation_recovered`
  list the oldest pairs a send superseded in `superseded_ids`. Nothing sets
  the revoked reason `reuse_detected` any more.
- **Logins while Moodle is down.** `POST /v3/auth/login` answers
  `503 moodle_unreachable` when the backend cannot reach Moodle to check
  the token, or Moodle answers with an error that is not about the token,
  as in maintenance mode; the backend logs its `errorcode` as
  `moodle.verify.moodle_error`. `401 moodle_verification_failed` now means
  only that Moodle refused the token (`invalidtoken`, or `accessexception`
  for an expired one), or that it belongs to another student. Both count
  against the login limit. After a 401, Android's automatic sign-in
  harvests a new token with the stored password; after a 503 it waits five
  minutes and tries again, as iOS does after either.
- **Settings.** `TIGERDUCK_AUTH_REFRESH_RECOVERY_WINDOW_SECONDS` is gone,
  and with it the way 3.3.1 gave to turn the recovery off; a line left in
  `.env` is ignored. `TIGERDUCK_AUTH_REFRESH_REUSE_GRACE_SECONDS` now only
  decides how a send of a replaced token is recorded: a refused one as
  `auth.refresh.duplicate_refused`, or as the `auth.refresh.reuse_detected`
  warning with its `reuse_detected_at`; one rotated again as
  `auth.refresh.grace_retry` or `auth.refresh.lost_rotation_recovered`. It
  no longer decides whether a token is rotated again or refused.
- **Rolling back, sessions.** After the stamp above, 3.3.5 takes a token
  this release rotated again for stolen when it comes back, and revokes the
  device's sessions as before. When one of three or more pairs one token
  was rotated into is used, the others stay live until they expire.
- **A device left without a session is asked to sign in again.** A device
  whose TigerSync session is gone, and whose app cannot sign in again by
  itself, stayed out of TigerSync while the app still showed the account as
  signed in, and nothing told the user (tigerduck-backend#125). Every 10
  minutes the backend now looks for a device announced in the last 3 days,
  with TigerSync on and an active push token, whose newest session (leaving
  out the rivals a duplicate refresh left, which the one used first
  superseded) did not end in a logout, and that either has no live session
  and was announced more than an hour after its newest session was revoked
  or expired, or has a live session not replaced for 24 hours before an
  announce. An announce under 10 minutes old waits for a later scan, so the
  refresh or sign-in the app makes right after it lands first. It files a
  `reauth_required` push with `reason: "session"` for that device alone,
  its title and body app-translation's `notification_reauth_session_*` in
  the device's language. A device is asked at most once every 7 days, and
  no more once its newest session ended more than 28 days before, after
  about four pushes; one with a live session it does not use is asked on.
  Pushes are filed only between 09:00 and 21:00 at school
  (`course_reminder_timezone`). Before each try to send it, a retry
  included, the pipeline checks the device again, and cancels the push as
  `stale_before_send` when the device signed in again, signed out, was
  removed or turned TigerSync off. A try that falls at night at school, as a
  retry after 21:00 or a job left pending over a restart, waits for 09:00,
  and one within 10 minutes of an announce waits out those 10 minutes;
  neither spends an attempt, and the device is checked again then. iPhone,
  iPad and Android; `server_push_enabled` is not read, as for the credential
  reauth push. On Android it goes at normal FCM priority, so a dozing phone
  gets it at its next maintenance window: the app shows nothing for it while
  signed out, and FCM demotes an app whose high-priority messages show
  nothing.
- **Expect a first round.** tigerduck-backend#125 counted about 115 such
  devices on 2026-10-21. The first daytime scan after the upgrade asks them,
  and one still without a session is asked again each week, until 28 days
  after its session ended.
- **Update the submodule.** Run `git submodule update --init` after
  `git pull`: this release pins `app-translation` to a commit with the new
  copy.
- **Migration `516f9bad27f0`** adds `user_devices.session_reauth_pushed_at`,
  null on every device, without rewriting the table, and the index
  `idx_auth_sessions_device` on `auth_sessions (device_id, id)`, which the
  scan reads each device's newest session by. Building the index blocks
  writes to `auth_sessions` while it runs, about half a second per million
  sessions, and the backend migrates before it starts. Schema only; a second
  run changes nothing.
- **Turning it off** without a rollback: set
  `TIGERDUCK_SESSION_REAUTH_SCAN_INTERVAL_SECONDS=0` in `.env` and run
  `./start.sh`. A push already filed still goes out.
- **Rolling back, pushes.** The stamp to `d899fe7d1f79` above also covers
  `516f9bad27f0`. 3.3.5 does not read the column or the index, and the next
  upgrade keeps the times the column recorded.
- No app update: both apps show a `reauth_required` push as a plain
  notification, and a tap opens the app. Android drops one that arrives
  while the app is signed out; an iPhone shows it even then, if its sign-out
  never reached the server.

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
