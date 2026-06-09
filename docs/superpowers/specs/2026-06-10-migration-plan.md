# Migration Plan

Date: 2026-06-10
Status: Approved

## Overview

Incremental migration in 4 phases. Each phase is independently deployable and does not break existing functionality. Tables are created early; traffic is switched later.

Total new tables: 22.

---

## New Tables Summary

| # | Table | Phase Created | Phase Activated |
|---|---|---|---|
| 1 | `users` | 1 | 1 |
| 2 | `external_accounts` | 1 | 1 |
| 3 | `external_account_credentials` | 1 | 1 |
| 4 | `auth_sessions` | 1 | 1 |
| 5 | `user_devices` | 1 | 1 |
| 6 | `device_push_tokens` | 1 | 1 |
| 7 | `push_jobs` | 1 | 4 |
| 8 | `push_deliveries` | 1 | 4 |
| 9 | `user_courses` | 2 | 2 (snapshot), 3 (server sync) |
| 10 | `user_course_overrides` | 2 | 2 |
| 11 | `user_course_skipped_dates` | 2 | 2 |
| 12 | `user_assignments` | 2 | 2 (snapshot), 3 (server sync) |
| 13 | `user_assignment_overrides` | 2 | 2 |
| 14 | `user_settings_documents` | 2 | 2 |
| 15 | `user_bulletin_subscriptions` | 2 | 2 |
| 16 | `user_bulletin_states` | 2 | 2 |
| 17 | `bulletin_user_matches` | 2 | 2 |
| 18 | `user_sync_state` | 2 | 2 |
| 19 | `user_change_log` | 2 | 2 |
| 20 | `sync_policies` | 3 | 3 |
| 21 | `sync_jobs` | 3 | 3 |
| 22 | `sync_runs` | 3 | 3 |

---

## Phase 1: Identity, Device, Auth Foundation

### New tables

`users`, `external_accounts`, `external_account_credentials`, `auth_sessions`, `user_devices`, `device_push_tokens`, `push_jobs`, `push_deliveries`

`push_jobs` and `push_deliveries` are created but not yet used — no push flow changes.

### New API endpoints

```
POST /v3/auth/login
POST /v3/auth/refresh
POST /v3/auth/logout
POST /v3/devices/register
GET  /v3/devices
DELETE /v3/devices/{id}
```

### Actions

- Frontend new version calls `/v3/auth/login` on login
- Backend creates `users` + `external_accounts` + encrypted credentials
- Backend creates `user_devices` and `device_push_tokens`
- Logged-in devices are also written to existing `device_registrations` (dual-write)
- All existing `/v2/*` endpoints remain unchanged
- Old frontend versions continue working on `/v2/*`

### Risk: Lowest

No impact on existing users. New tables are additive.

### Rollback

Drop new tables, remove `/v3/auth/*` routes. No data loss — existing flow unaffected.

---

## Phase 2: User-Scoped Data Snapshot + Cross-Device Sync

### New tables

`user_courses`, `user_course_overrides`, `user_course_skipped_dates`, `user_assignments`, `user_assignment_overrides`, `user_settings_documents`, `user_bulletin_subscriptions`, `user_bulletin_states`, `bulletin_user_matches`, `user_sync_state`, `user_change_log`

### New API endpoints

```
GET  /v3/sync?since_revision=&limit=
GET  /v3/sync/full
POST /v3/sync/initial-upload

GET  /v3/courses?semester=
GET  /v3/assignments
PUT  /v3/assignments/{id}/override
PUT  /v3/courses/{id}/override

GET  /v3/settings/{namespace}
GET  /v3/settings?namespaces=
PUT  /v3/settings/{namespace}

GET    /v3/bulletin-subscriptions
POST   /v3/bulletin-subscriptions
PATCH  /v3/bulletin-subscriptions/{id}
DELETE /v3/bulletin-subscriptions/{id}

GET  /v3/bulletin-states?bulletin_ids=
PUT  /v3/bulletin-states/{bulletin_id}
```

### Actions

- On first login after upgrade, frontend uploads local data via `/v3/sync/initial-upload`:
  - Local courses → `user_courses` (as snapshot; `source = 'ntust_portal'` or `'user_added'`)
  - Local assignments → `user_assignments` (as snapshot from cached data)
  - Assignment overrides (archived, locally_completed) → `user_assignment_overrides`
  - Course customizations (colors, names, skipped dates) → `user_course_overrides` + `user_course_skipped_dates`
  - Settings (home layout, preferences) → `user_settings_documents`
  - Bulletin subscriptions → copy from device-level `bulletin_subscriptions` to `user_bulletin_subscriptions`
- Subsequent changes write to both local storage and backend
- Other devices pull changes via `/v3/sync` incremental API
- Server-side sync is NOT enabled yet — `user_courses` and `user_assignments` only contain client-uploaded snapshots

### Risk: Medium

Requires frontend changes. No impact on users who don't upgrade. Login-gated — anonymous users unaffected.

### Rollback

New frontend can fall back to local-only mode if backend sync is unavailable.

---

## Phase 3: Server-Side Academic Sync

### New tables

`sync_policies`, `sync_jobs`, `sync_runs`

### Actions

- Seed `sync_policies` with default intervals:
  - `moodle_assignments`: 28800s, enabled
  - `ntust_courses`: 28800s, disabled (enable via dashboard during course selection)
  - `calendar`: 604800s, enabled
  - `grades`: 28800s, disabled
- Create `sync_jobs` for each user with active `external_accounts`
- Backend scheduler starts executing sync jobs:
  - Fetches Moodle assignments using stored tokens
  - Fetches NTUST courses using stored credentials (when enabled)
  - Upserts `user_courses` and `user_assignments`
  - Appends changes to `user_change_log`
- Frontend switches from local API calls to backend data:
  - `GET /v3/assignments` replaces direct Moodle API calls
  - `GET /v3/courses` replaces direct NTUST course selection scraping
- Pull-to-refresh triggers backend immediate sync:
  - `POST /v3/sync-jobs/run-now?job_type=moodle_assignments`
  - Frontend does NOT directly call school APIs
- Dashboard endpoint for admin:
  - `PATCH /v3/admin/sync-policies/{job_type}` to adjust intervals and active windows

### Risk: Medium-High

Involves storing and using encrypted credentials. Requires:
- Encryption key management (env var or KMS)
- Credential failure notification mechanism
- Rate limiting / staggering of school API requests
- Monitoring for sync job failures and stale locks

### Rollback

Disable all `sync_policies`. Frontend can fall back to direct school API calls temporarily.

---

## Phase 4: Server-Authoritative Push

### Tables activated

`push_jobs`, `push_deliveries` (created in Phase 1)

### Actions

Gradual migration of push notification sources:

#### Step 4a: Assignment Reminders

- Backend sync job detects upcoming assignment due dates
- Checks `user_assignment_overrides` to exclude completed/ignored
- Creates `push_jobs` with appropriate dedupe keys and fire_at times
- `push_deliveries` fan-out to all active `device_push_tokens`
- Frontend stops creating local assignment reminders for logged-in users

#### Step 4b: Course Reminders

- Backend computes class start times from `user_courses.schedule_json`
- Creates `push_jobs` for course start notifications
- Existing `scheduled_pushes` usage stops for logged-in users
- Frontend stops POSTing to `/v2/schedule/sync` for logged-in users

#### Step 4c: Bulletin Notifications

- User-level bulletin dispatch uses `bulletin_user_matches` + `push_jobs`
- Does not update `bulletin.notified_at` (reserved for anonymous flow)
- Anonymous device bulletin dispatch continues using existing `bulletin_dispatches`

#### Step 4d: Live Activity

- Live Activity tokens registered via `device_push_tokens` (token_kind = 'live_activity_update')
- Existing `live_activity_update_tokens` kept as read fallback during transition
- After full migration, `live_activity_update_tokens` deprecated

### Risk: Medium

Push is the most user-sensitive feature. Requires:
- Parallel running during transition (both old and new pipelines)
- Monitoring push delivery rates
- Fallback to old pipeline if new pipeline has issues
- Per-user gradual rollout (not all users at once)

### Rollback

Re-enable old push pipelines per step. Each step is independently reversible.

---

## Existing Table Disposition

| Existing Table | Phase 1 | After Phase 4 |
|---|---|---|
| `device_registrations` | Kept, dual-write for logged-in | Kept, anonymous devices only |
| `scheduled_pushes` | Kept | Logged-in: deprecated. Anonymous: kept until anonymous reminders are retired |
| `live_activity_update_tokens` | Kept, read fallback | Deprecated after Live Activity migration |
| `bulletin_subscriptions` | Kept | Kept, anonymous devices only |
| `bulletin_dispatches` | Kept | Kept, anonymous devices only |
| `custom_push_dispatches` | Kept | Integrated into push_jobs |
| `device_lists` / `device_list_members` | Kept | Kept (operator tool) |
| `bulletins` | Unchanged | Unchanged |

No existing tables are dropped. Anonymous device support is preserved indefinitely.

---

## Key Constraints

- NTUST SSO rate-limits per IP: backend never does proxy SSO login. App handles SSO, backend only stores credentials and uses them for Moodle token refresh (infrequent).
- School API response time is slow: backend caches data, frontend reads from backend cache.
- Sync job staggering: `FOR UPDATE SKIP LOCKED` + batch limit + natural `run_after` distribution prevents overwhelming school servers.
- Encryption key rotation: `external_account_credentials.version` and `encryption_key_id` support key rotation without re-encrypting all rows at once.
