# TigerDuck backend `/v4` contract

`/v4` is TigerSync protocol 2, which `/v3` already serves to requests sending `X-TigerSync-Protocol: 2` (`server/sync/merge.py`, `server/sync/membership.py`), plus the owner's four changes.

## 1. `/v3` endpoints the Apple app calls

iOS means iPhone and iPad; Mac is the native macOS build. The Watch and the extensions never call the backend.

On every Bearer endpoint: 401 `missing_bearer_token`, `invalid_access_token` or `session_revoked`. Everywhere: 422 `{"detail":[{type,loc,msg}]}` and a plain-text 500. Other errors are `{"detail":"<code or prose>"}` unless shown. Every response carries `X-TigerSync-Clock`.

| # | Method, path | Auth | Request | 2xx | Other statuses | Client |
|---|---|---|---|---|---|---|
| 1 | POST `/auth/login` | none | `student_id, moodle_token, moodle_private_token?, device_info{client_device_id, platform, device_class?, app_version?, os_version?, device_model?}` | `access_token, refresh_token, token_type, expires_in, user{id, student_id, display_name}, device_id` | 400 `moodle_token_required`, 401 `moodle_verification_failed`, 403 `account_suspended`, 409 `login_conflict_retry`, 429, 503 `moodle_unreachable` | iOS, Mac |
| 2 | POST `/auth/refresh` | token in body | `refresh_token` | new pair, `expires_in` | 401 `invalid_refresh_token`, `refresh_token_expired`, `session_revoked`; 429 (10/min/IP, prose) | iOS, Mac |
| 3 | PATCH `/auth/credentials` | Bearer | `moodle_token, moodle_private_token?` | `updated` | 429 | iOS, Mac |
| 4 | POST `/devices/register` | Bearer | `device_info` fields, `push_token{provider, token_kind, token_value, bundle_id, environment, scope_key}?`, `cloud_sync_enabled?, bulletin_push_enabled?, server_push_enabled?, locale?` | `device_id, push_token_id` | | iOS, Mac |
| 5 | POST `/devices/anonymous` | none | `device_id, platform:"apple", device_class, push_token?, bundle_id, server_push_enabled?` | `device_id, registered` (unread) | 429 | iOS, Mac |
| 6 | DELETE `/devices/{client_device_id}` | Bearer (also a departing session) | | 204 | 404 `"device not found"` | iOS, Mac |
| 7 | PATCH `/devices/{client_device_id}/preferences` | Bearer | any of nine `sync_*`/`*_enabled` flags, `locale?` | `device_id` + all flags | 404 `device_not_found` | iOS, Mac |
| 8 | GET `/sync/revision` | Bearer | | `revision` | | iOS, Mac |
| 9 | GET `/sync?since_revision&limit` | Bearer | | `current_revision, returned_until_revision, has_more, changes[{revision, entity_type, entity_id, operation, payload}]` | 410 `{error:"sync_revision_expired", min_available_revision, current_revision, full_sync_required}` | iOS, Mac |
| 10 | GET `/sync/full` | Bearer | | revision, `server_hlc`, resets, tombstones, courses, overrides, skipped dates, assignments, settings documents, holidays, `roster_requests`; without the protocol header (iOS today) 30 days of tombstones with `deleted_by_reset, deleted_by_this_device` | | iOS, Mac |
| 11 | POST `/sync/courses/upload` | Bearer; protocol 2 with `adds` | `courses[{semester, course_no, course_name, moodle_id?, credits?, classroom?, instructors, schedule_json, classroom_map, …}], course_overrides[{course_key, color_hex}], force_keys, adds?[{course_key, hlc}], roster_terms?` | `upserted, skipped_tombstoned, overrides_applied, accepted_keys, adds?` | 409 `clock_ahead`, 422 `add_not_in_courses` | iOS; Mac with course sync |
| 12 | DELETE `/sync/courses?semester&hlc` | Bearer | | `deleted, reset_hlc, applied?` | 400 `device_id_required`, 409 | iOS, Mac |
| 13 | DELETE `/sync/courses/{course_key}?hlc&kind` | Bearer | | `deleted, applied, hlc` | 409 | iOS, Mac |
| 14 | POST `/sync/assignments/upload` | Bearer | `assignments[{moodle_assignment_id, course_no, course_name, title, due_at?, moodle_url?, is_submitted, grade?}]` | `upserted` | | iOS, Mac |
| 15 | PATCH `/sync/assignments/{moodle_assignment_id}/override` | Bearer | `local_status, hlc?` | set: `id, updated_at, local_status, local_status_hlc, applied`; clear: `deleted:true` + last three | 404 `assignment_not_found`, 409 | iOS, Mac |
| 16 | PATCH `/sync/courses/{moodle_id}/override` | Bearer | `color_hex?, custom_name?, locale?, ignore_assignments?, moodle_course_id?, hlc?` | values, `*_hlc`, `applied[]` | 404 `course_not_found`, 409 | iOS, Mac |
| 17 | PUT `/sync/holiday-overrides/{holiday_id}` | Bearer | `notify, hlc?` | `holiday_id, notify, hlc, applied` | 404 `holiday_not_found` | iOS, Mac |
| 18 | GET `/settings/{namespace}` | Bearer | | `namespace, schema_version, document, revision, updated_at` | 404 (never written), 422 | iOS |
| 19 | PUT `/settings/{namespace}` | Bearer | `schema_version, document, base_revision\|null` | as 18 | 409 `{error:"settings_conflict", namespace, server}` | iOS |
| 20 | POST `/schedule/sync` | Bearer | `events[{source_id, scenario, fire_at, snapshot}]` (≤200), replaces the device's list | `pending, replaced` | 400 `device_id_required` | iOS |
| 21 | POST `/live-activities/register` | Bearer | `activity_id, source_id, update_token_hex, countdown_target, snapshot, bundle_id, environment?` | `token_id, end_job_id` | 400, 422 `activity_id_mismatch` | iOS |
| 22 | GET `/bulletins/taxonomy` | none | | `orgs, tags, default_tags` | | iOS, Mac |
| 23 | GET `/bulletins?limit&cursor&include_deleted` | none | | `items, next_cursor` | | iOS, Mac |
| 24 | GET `/bulletins/{id}` | none | | detail | 404 (prose) | iOS, Mac |
| 25 | GET `/bulletin-subscriptions` | Bearer | | `items[{id, name, orgs, tags, mode, enabled, revision}]` | 400 `device_id_required` | iOS |
| 26 | PUT `/bulletin-subscriptions` | Bearer | `rules[{name, orgs, tags, mode, enabled}]` | `items`; every rule re-created with a new id | 400 | iOS |
| 27 | GET `/calendar/semesters` | none, ETag | | terms, holidays | 304 | iOS, Mac |
| 28 | GET `/calendar/course-changes?term` | none, ETag | | `term, changes` | 304, 404 `term_not_found` | iOS, Mac |
| 29 | GET `/course-catalog/{semester}?language` | Bearer, ETag, gzip | | snapshot | 304, 404 `course_catalog_not_found` | iOS, Mac |

Unversioned and untouched: `/ping`, `/links/{name}`, `/.well-known/tigerduck/tls-pins.json`.

## 2. The `/v4` contract

### Mapping

Protocol 2 is implied on `/v4`: the header is ignored, every write that takes `hlc` must send it (422 `hlc_required`), and protocol 2's last-writer-wins rules apply (`server/AGENTS.md`).

| v3 | v4 | Change |
|---|---|---|
| 1–8, 14, 18, 19, 21–24, 27–29 | same path under `/v4` | envelope only |
| 20 schedule/sync | same | envelope only; the client skips unchanged lists |
| 11 courses/upload | same | changed |
| 16 `courses/{moodle_id}/override` | `courses/{course_key}/override` | renamed, changed |
| 15 assignment override | same | changed |
| 12, 13, 17 deletes, holiday PUT | same | changed: `hlc` required |
| 9 `/sync`, 10 `/sync/full` | same | changed |
| 25, 26 subscriptions | same | changed |

Changed: 10 endpoints (9–13, 15–17, 25, 26). Dropped: the per-rule subscription routes, which Android leaves for the `client_id` PUT (Q1).

### Changed endpoints

**Course upload.** Request `courses`, `adds[{course_key, hlc}]` (≤50, every hand-added course listed), `roster_terms`. Sending `course_overrides` or `force_keys` is 422 `invalid_request`. Response `upserted, skipped_tombstoned, accepted_keys, adds[{course_key, applied, hlc}]`. Why: colours take two routes that disagree on both apps. iOS uploads every colour while TigerSync is on, ignoring the colour switch, while its PATCH queue follows it. Android uploads `course_overrides.color_hex` for every custom colour, gated only on the master and course switches, while its PATCH queue follows the colour switch.

**Course override, `PATCH /sync/courses/{course_key}/override`.** The key is `client:{semester}:{course_no}`, which both apps build. Body `color_hex?, custom_name?, locale?, ignore_assignments?, moodle_course_id?, hlc` (needed with any value). Response as v3. Why: iOS can PATCH only courses with a Moodle id, so other colours reach the backend only through the upload. A `moodle_id` also names no term, and the lookup picks one row by preference (`server/routes/overrides.py`, `_get_course_by_moodle_id`). The override row points at the course row, so the client sends the PATCH after the upload that adds the course, and retries a 404 `course_not_found` after the next upload. On both apps the colour switch needs the course switch, so the row exists.

**Assignment override.** Body `local_status, hlc`. Every reply, a clear included, is `{local_status, local_status_hlc, applied}`. Why: v3 answers a clear in another shape, so iOS does not read the reply.

**Deletes and holiday PUT.** These are v3's protocol-2 forms with `hlc` required. The client answers a 409 `clock_ahead` by stamping the edit again from the server's clock and resending it, which replaces iOS's retry without a stamp and its protocol header, sent today only with adds.

**`GET /sync/full`.** Always the protocol-2 shape: all tombstones with `hlc, kind`, `term_resets`, `courses_reset_hlc`, `server_hlc`, and no 30-day window, `deleted_by_reset` or `deleted_by_this_device`. `course_overrides[]` gains `course_key`.

**`GET /sync`.** An expired revision is 409 `sync_revision_expired` with the same fields. Why: iOS latches its update screen on any 410, so this answer cannot be one.

**Subscriptions.** Each rule carries `client_id`, a UUID the client makes. GET returns `items[{id, client_id, name, orgs, tags, mode, enabled, revision}]`. `PUT {rules[{client_id, …}]}` updates this device's live row with that `client_id` in place (its `id` kept), inserts new ones and soft-deletes the rest. A duplicate `client_id` is 422. Why: v3 re-creates every rule, and iOS copies ids back by array index.

**Schedule sync.** No wire change. The client skips a post whose canonical body hash equals its last 2xx in this session. The backend's half already holds: an identical list is a no-op (`server/routes/schedule_v3.py` keeps a job whose key and fire time match). A device's pending starts change only by its own post, by firing, by device deletion or by server cancels, and a resend restores none of these.

### Error envelope

Every non-2xx JSON body on `/v4`, including unknown routes and 500s:

```json
{"error": "clock_ahead", "message": "English, for logs", "…": "code-specific fields"}
```

Clients branch on `error` only. Extra fields sit at the top level, as in v3's `settings_conflict` and `sync_revision_expired`, which already fit. `detail` is gone, and v3 prose becomes codes.

| Status | Codes |
|---|---|
| 400 | `device_id_required`, `moodle_token_required` |
| 401 | `missing_bearer_token`, `invalid_access_token`, `session_revoked`, `session_user_mismatch`, `invalid_refresh_token`, `refresh_token_expired`, `moodle_verification_failed` |
| 403 | `account_suspended` |
| 404 | `not_found`, `device_not_found`, `course_not_found`, `assignment_not_found`, `holiday_not_found`, `settings_document_not_found`, `subscription_not_found`, `bulletin_not_found`, `term_not_found`, `course_catalog_not_found` |
| 409 | `clock_ahead`, `login_conflict_retry`, `settings_conflict` (+`namespace, server`), `subscription_conflict` (+`server`), `sync_revision_expired` (+revisions) |
| 410 | `api_retired` (+`current_api`), `app_version_retired` |
| 422 | `invalid_request` (+`fields[{type, loc, msg}]`), `hlc_required`, `add_not_in_courses`, `activity_id_mismatch`, `unknown_namespace` |
| 429 | `too_many_login_attempts`, `too_many_refresh_attempts`, `too_many_credential_updates`, `too_many_registration_attempts` |
| 500 | `internal_error` |
| 503 | `auth_not_configured`, `moodle_unreachable` |

### Auth and refresh

- Same Bearer JWT and `auth_sessions`. A pair from `/v3` works on `/v4` and back, so updating signs nobody out.
- On 401 `invalid_access_token`, refresh once and replay once. On `session_revoked` or `session_user_mismatch`, sign in again; a refresh would only spend a use.
- Refresh is unchanged. A token sent again while its successor is unused is rotated again, keeping at most 16 unused sessions (`_UNUSED_SESSIONS_KEPT`). Once a successor is used, it gets 401 `session_revoked`. The limit is 10 per minute per IP, shared with `/v3`.
- The client sends one refresh token at most twice, counting sends that may have reached the server, and stores the count with the tokens. After that it signs in again.

### Versioning and retirement

- `X-TigerSync-Clock` is on every response, errors too.
- Fields may be added within `/v4`, and clients ignore unknown ones. A removal or rename needs `/v5`.
- A retired API gives 410 `{"error":"api_retired","message":…,"current_api":…}` on every path.
- A device below the minimum supported app version gets 410 `{"error":"app_version_retired","message":…}`.
- The v4 client latches the update screen only on those two codes.
- When `/v3` retires, its 410 keeps today's `detail` and `current_api` and adds `"error":"api_retired"`.

### Push payloads per API version

They are identical, and the backend does not track a device's API version. No v4 change touches a payload: `sync_trigger` carries `source_device_id`, a Live Activity's content-state is the device's own snapshot (dates converted in `server/push/payload.py`), and bulletin and reminder payloads carry ids shared by both versions. Two rules follow:

- 4.0.0 must decode every v3 payload, including jobs its 3.x self queued before the update (starts filed up to 48 hours ahead, Live Activity ends).
- `/v4` keeps the snapshot keys and the ISO `countdownTarget`/`progressStart` that `_normalize_snapshot_for_apns` converts.

## 3. Keeping `/v3` unchanged

`/v4` mounts the same handlers with `ClockProtocolDep` forced true, which alone makes 12, 13 and 17 require `hlc`; 9, 10, 11, 15, 16, 25 and 26 get `/v4` variants. Both versions share one database: users and auth (`users`, `auth_sessions`, `user_devices`, `device_push_tokens`, `device_registrations`), courses (`user_courses`, `user_course_tombstones`, `user_semester_resets`, `user_course_rosters`, `user_course_overrides`), assignments (`user_assignments`, `user_assignment_overrides`), `user_holiday_overrides`, `user_settings_documents`, sync state (`user_sync_state`, `user_change_log`), `user_bulletin_subscriptions` and push (`push_jobs`, `push_deliveries`). These must not drift:

- **Change log.** A v4 write appends the same entries and queues the same `sync_trigger` as its v3 twin. One account can have Android or a 3.x Mac on `/v3` beside a 4.0 iPhone.
- **Merge.** `apply_field`, `membership` and the clock code are shared. v3 requests without the header keep arrival stamps and `force_keys`, and `_released_tombstones` still leaves out removals beside a roster row from v3 snapshots.
- **Colours.** The v3 upload keeps its fill-only colours. The v4 upload writes none.
- **Subscriptions.** Add `client_id uuid NOT NULL DEFAULT gen_random_uuid()`, backfilled, unique on `(device_id, client_id) WHERE deleted_at IS NULL`. The v3 PUT and Android's per-rule POST get ids from the default. The v3 serializer stays as it is.
- **Errors.** Routes keep today's `HTTPException` details. One handler builds the envelope for `/v4` paths, mapping prose to codes, so `/v3` bodies stay byte for byte the same.
- **Auth.** Same JWT secret, HMAC key and limiter instances, so `/v4` adds no second budget.
- **Middleware.** The clock and 410 middleware cover both prefixes, and `/version` gains `api_v4_base_path`.
- **Tests.** The v3 suite runs unchanged. Add v4 contract tests and cross-version ones: write on one version, then read the feed and snapshot on the other.

## 4. Decisions

1. **Subscriptions.** Settled: one `client_id` PUT. Android moves to it on `/v4` and stores a `client_id` per rule.
2. **Which routes `/v4` mounts.** Settled: the 29 above, which cover every route Android calls. Left out: the per-rule subscription routes, `initial-upload`, `academics.py`, `bulletin-states`, `GET /devices`, `/auth/logout`, `DELETE /schedule/{id}`, `GET /sync/holiday-overrides` and `/sync-jobs/run-now`.
3. **`sync_revision_expired` is 409**, so every 410 means "update the app" even when the body is unreadable.
4. **No server-side schedule digest.** The client skip is enough, and a resend is already a no-op.
5. **`course_key` in the override path.** Settled: Android builds the key already.
6. **The refresh limit stays at 2.** A comment in the Apple app's token manager says a third use revokes the device's sessions, which has not been true since tigerduck-backend#123; the refactor fixes that comment.
7. **`/v3` retires with a path-wide 410 `api_retired`**, at least a month after 4.0.0, once Android is on `/v4` and `user_devices.app_version` shows no active 3.x device.
