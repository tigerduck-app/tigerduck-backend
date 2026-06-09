# Sync & Push Architecture Spec

Date: 2026-06-10
Status: Approved

## Overview

Defines the auth flow, cross-device sync strategy, server-side academic data refresh, push notification pipeline, and client sync API.

---

## 1. Authentication Flow

### Login

```
1. App completes NTUST SSO login + obtains Moodle token (client-side)
2. POST /v3/auth/login
   body: { student_id, password, moodle_token, moodle_private_token, device_info }
3. Backend:
   a. Lightweight Moodle API call to verify token is valid and matches student_id
      (does NOT do SSO login — NTUST SSO rate-limits per IP)
   b. Find or create users + external_accounts records
   c. Encrypt and store credential blob (password + token_cache with expires_at)
   d. Create/upsert user_devices record
   e. Issue JWT access token (15min) + refresh token (90 days)
   f. Return { access_token, refresh_token, user, device_id }
4. App stores tokens. Subsequent API calls use Authorization: Bearer <access_token>
```

### Token Refresh

```
1. POST /v3/auth/refresh
   body: { refresh_token }
2. Backend:
   a. HNAC-Hash256 the refresh token, look up auth_sessions
   b. If valid and not revoked:
      - Revoke old session (revoked_at, revoked_reason='rotated', replaced_by_session_id)
      - Create new session with fresh refresh_token_hash
      - Issue new access + refresh tokens
   c. If already revoked (reuse detection):
      - Set reuse_detected_at on the revoked session
      - Revoke ALL sessions for that device
      - Return 401
   d. If expired: return 401
```

### Logout

```
POST /v3/auth/logout
- Revoke current session (revoked_reason='logout')
- Optionally revoke all sessions for this device
- Do not delete user_devices record (device may re-login)
```

### Anonymous Devices

Anonymous devices (not logged in) continue using the existing `/v2/devices` registration flow and `device_registrations` table. They can view bulletins and calendar but cannot access courses, assignments, sync, or user-level push.

---

## 2. Cross-Device Sync Strategy

### Merge-Based Per-Field Sync

For entities where individual fields are independently editable (course overrides, assignment overrides, bulletin states), each field has its own `updated_at` timestamp and `device_id`.

When syncing:
1. Client sends changed fields with their timestamps
2. Server compares per-field: if client timestamp > server timestamp, accept client value
3. Server returns the merged result

This prevents one device's changes from overwriting another device's unrelated changes.

### Revision-Based Optimistic Concurrency

For settings documents (JSONB blobs where fields are typically edited together on one settings screen), use revision-based optimistic concurrency:

1. Client reads document with its current `revision`
2. Client makes changes, submits with `base_revision`
3. Server accepts if `base_revision == current_revision`, then increments revision
4. On mismatch: returns 409 Conflict with server's current document
5. Client resolves conflict (merge or overwrite) and resubmits

### Independent Entities

For entities that are individually created/deleted (skipped dates, bulletin subscriptions), each row is an independent sync unit. Natural merge: create on one device, create on another — both survive. Delete uses soft-delete with `deleted_at`.

---

## 3. Change Log & Incremental Sync

### Write Path

Every change to user-visible data appends to `user_change_log` and updates `user_sync_state.current_revision`:

```sql
INSERT INTO user_change_log (user_id, entity_type, entity_id, operation, payload, device_id)
VALUES (:user_id, :entity_type, :entity_id, :operation, :payload, :device_id)
RETURNING revision;

UPDATE user_sync_state
SET current_revision = :revision, updated_at = now()
WHERE user_id = :user_id;
```

This includes server-side sync updates (new assignments, course changes).

### Read Path (Incremental)

```http
GET /v3/sync?since_revision=123&limit=500
```

Response:

```json
{
  "current_revision": 620,
  "returned_until_revision": 500,
  "has_more": true,
  "changes": [
    {
      "revision": 124,
      "entity_type": "assignment",
      "entity_id": "456",
      "operation": "upsert",
      "payload": { "fields": ["title", "due_at"] }
    }
  ]
}
```

Client processes changes by calling the appropriate GET API for each entity, then stores `returned_until_revision` as the next `since_revision`.

### Full Sync

When `since_revision < compacted_revision` or on first login:

```http
GET /v3/sync/full
```

Returns all user-scoped data as a snapshot plus `current_revision`. Must use a repeatable-read transaction to ensure snapshot consistency.

### Expiry Detection

```http
GET /v3/sync?since_revision=50
→ 410 Gone
{
  "error": "sync_revision_expired",
  "min_available_revision": 200,
  "current_revision": 620,
  "full_sync_required": true
}
```

### Initial Upload

On first login, client uploads its local data:

```http
POST /v3/sync/initial-upload
```

Uploads local courses, assignments (as snapshots), overrides, settings, and bulletin subscriptions.

### Retention

Changelog entries older than 30 days are deleted. `user_sync_state.compacted_revision` is updated to the max deleted revision per user.

---

## 4. Server-Side Academic Data Refresh

### Sync Job Execution

```
Scheduler tick (every 30 seconds):
  1. Query due sync_jobs:
     WHERE status = 'pending' AND run_after <= now()
     ORDER BY priority, run_after
     LIMIT 5
     FOR UPDATE SKIP LOCKED

  2. Also recover stale jobs:
     WHERE status = 'running' AND locked_at < now() - INTERVAL '10 minutes'

  3. Lock: status = 'running', locked_by = worker_id, locked_at = now()
  4. Create sync_runs row
  5. Decrypt credentials → get Moodle token from cache
  6. If token expired → use NTUST password to re-obtain token → update credential blob
  7. Call Moodle/NTUST API → upsert user_assignments / user_courses
  8. Changed entities → append user_change_log
  9. Update sync_runs: status, fetched_count, changed_count
 10. Update sync_jobs:
     - Success: status = 'pending', run_after = now() + interval, attempts = 0
     - Retriable failure: status = 'pending', run_after = now() + backoff, attempts++
     - Max attempts exceeded: status = 'failed'
     - Credentials invalid: status = 'disabled', notify user to reauthorize
```

### Refresh Frequencies

| job_type | Default interval | Special conditions |
|---|---|---|
| `moodle_assignments` | 8 hours | Event-driven trigger before sending push notifications |
| `ntust_courses` | Dashboard-configurable | Active only during course selection/dropout periods (8h when active) |
| `calendar` | 7 days | Admin manual trigger via dashboard |
| `grades` | 8 hours | Disabled by default until schema is implemented |

Assignments and courses use the same student session, so they are fetched together when both are due.

### Request Staggering

All user sync requests originate from one server IP. Jobs are staggered by:
- `FOR UPDATE SKIP LOCKED` limits concurrent execution
- Batch size limit (5 jobs per tick)
- Natural distribution of `run_after` times across users

### Credential Failure Handling

When credentials become invalid (password changed, account locked):
1. `external_accounts.credential_status = 'invalid'`
2. `sync_jobs.status = 'disabled'` for all job types
3. Push notification to user: "Please re-login to continue syncing"
4. Backend stops attempting sync until user re-authenticates

### Pull-to-Refresh

Frontend pull-to-refresh does NOT directly call school APIs. Instead:

```http
POST /v3/sync-jobs/run-now?job_type=moodle_assignments
```

Backend queues a high-priority sync job (priority = 1). Client polls sync status or waits for `/v3/sync` delta.

If an identical job is already running or pending with priority <= 1, return existing job status instead of creating another run.
Apply per-user cooldown, e.g. one manual run per 60 seconds per job_type.

If sync fails, backend returns appropriate error:
- `credential_invalid`: user must re-login
- `school_rate_limited`: try again later
- `sync_failed`: transient error

---

## 5. Push Notification Pipeline

### Two-Phase Delivery

```
Phase 1: Materialize
  1. Scheduler scans push_jobs WHERE status='pending' AND fire_at <= now() AND available_at <= now()
  2. Lock job: status = 'processing'
  3. Query user's active device_push_tokens (status='active', not expired)
  4. Create push_deliveries rows (one per token)

Phase 2: Deliver
  5. For each pending delivery:
     - Send via APNs or FCM
     - Update delivery status: sent / failed / skipped
     - On 410 BadDeviceToken: mark device_push_tokens.status = 'invalidated'
  6. Aggregate push_jobs.status:
     - All sent/skipped (at least one sent) → 'sent'
     - Mix of sent and failed → 'partial_failed'
     - All failed → 'failed'
     - No active tokens → 'failed', last_error = 'no_active_tokens'
```

### Push Sources

| Source | Trigger | Channel |
|---|---|---|
| Assignment reminder | Sync job detects upcoming due date | `assignment` |
| Course start | Server-computed from schedule | `course` |
| New bulletin | Bulletin dispatch job matches user | `bulletin` |
| Credential invalid | Sync job credential failure | `system` |
| Custom push | Admin via portal | `custom` |

### Bulletin Dispatch Flow

```
1. Query processed bulletins (from existing bulletin pipeline)
2. For each bulletin:
   a. Load all enabled user_bulletin_subscriptions
   b. Run matcher (existing matcher.py logic)
   c. INSERT bulletin_user_matches ON CONFLICT DO NOTHING
   d. Query matches WHERE pushed_at IS NULL
   e. Create push_jobs (dedupe_key prevents duplicates)
   f. On push_job creation: set bulletin_user_matches.push_job_id and pushed_at
3. User-level flow does NOT update bulletins.notified_at (reserved for anonymous device flow)
```

### Compatibility with Existing Push

During migration:
- Anonymous devices: existing `device_registrations` + `bulletin_dispatches` + `scheduled_pushes`
- Logged-in users: new `push_jobs` + `push_deliveries` + `device_push_tokens`
- `PushRouter` queries both systems based on whether the target has a `user_id`

---

## 6. Settings Sync API

### Write

```http
PUT /v3/settings/{namespace}
```

Request:

```json
{
  "schema_version": 1,
  "document": { "sections": [...] },
  "base_revision": 3
}
```

Success (200):

```json
{
  "namespace": "home_layout",
  "schema_version": 1,
  "revision": 4,
  "document": { "sections": [...] },
  "updated_at": "2026-06-10T00:00:00Z"
}
```

Conflict (409):

```json
{
  "error": "settings_conflict",
  "namespace": "home_layout",
  "server": {
    "schema_version": 1,
    "revision": 5,
    "document": { ... }
  }
}
```

Create: `base_revision = null` and namespace doesn't exist → creates. `base_revision = null` but namespace exists → 409.

### Read

```http
GET /v3/settings/{namespace}
GET /v3/settings?namespaces=home_layout,appearance,notification
```

### On successful write

Append to `user_change_log`:

```json
{
  "entity_type": "settings_document",
  "entity_id": "home_layout",
  "operation": "upsert",
  "payload": { "revision": 4 }
}
```

Payload is a pointer only — never the full document.

---

## 7. Bulletin Subscription API

Per-rule CRUD, not bulk replace.

```http
GET    /v3/bulletin-subscriptions
POST   /v3/bulletin-subscriptions
PATCH  /v3/bulletin-subscriptions/{id}
DELETE /v3/bulletin-subscriptions/{id}
```

PATCH uses optimistic concurrency:

```json
{
  "base_revision": 3,
  "name": "教務處重要公告",
  "orgs": ["教務處"],
  "tags": ["修課"],
  "mode": "AND",
  "enabled": true
}
```

On success: `revision = revision + 1`. On mismatch: 409 with current server state.

Anonymous devices continue using existing `/v2/devices/{device_id}/subscriptions`.
