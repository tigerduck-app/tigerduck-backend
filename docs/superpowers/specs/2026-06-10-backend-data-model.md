# Backend Data Model Spec

Date: 2026-06-10
Status: Approved

## Overview

22 new tables to support user accounts, cross-device sync, server-side academic data refresh, and multi-platform push notifications. Organized into 6 layers.

---

## 1. Identity & Auth

### `users`

```sql
CREATE TABLE users (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    student_id    TEXT,
    display_name  TEXT,
    status        TEXT NOT NULL DEFAULT 'active',
    locale        TEXT DEFAULT 'zh-Hant',
    last_login_at TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at    TIMESTAMPTZ,

    CONSTRAINT chk_users_status
        CHECK (status IN ('active', 'suspended', 'pending_deletion'))
);

CREATE UNIQUE INDEX ux_users_student_id_active
    ON users (student_id)
    WHERE student_id IS NOT NULL AND deleted_at IS NULL;
```

`student_id` is a denormalized cache of the primary NTUST account. The authoritative source is `external_accounts(provider='ntust_sso', external_user_id)`.

### `external_accounts`

```sql
CREATE TABLE external_accounts (
    id                   BIGSERIAL PRIMARY KEY,
    user_id              UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider             TEXT NOT NULL,
    external_user_id     TEXT NOT NULL,
    credential_status    TEXT NOT NULL DEFAULT 'active',
    last_auth_success_at TIMESTAMPTZ,
    last_auth_failure_at TIMESTAMPTZ,
    last_auth_error      TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (provider, external_user_id),
    UNIQUE (user_id, provider),

    CONSTRAINT chk_credential_status
        CHECK (credential_status IN ('active', 'expired', 'invalid', 'revoked'))
);
```

When user is hard-deleted, `ON DELETE CASCADE` removes external accounts.

### `external_account_credentials`

Envelope encryption. DB stores ciphertext only. Decryption key lives in env var or KMS.

```sql
CREATE TABLE external_account_credentials (
    external_account_id  BIGINT PRIMARY KEY
        REFERENCES external_accounts(id) ON DELETE CASCADE,
    encryption_algorithm TEXT NOT NULL DEFAULT 'AES-256-GCM',
    encryption_key_id    TEXT NOT NULL,
    ciphertext           BYTEA NOT NULL,
    nonce                BYTEA NOT NULL,
    aad                  TEXT NOT NULL,
    version              INTEGER NOT NULL DEFAULT 1,
    rotated_at           TIMESTAMPTZ,
    last_used_at         TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

AAD format: `external_account:{id}:provider:{provider}`

Decrypted credential blob structure:

```json
{
  "ntust_password": "...",
  "token_cache": {
    "moodle_token": "...",
    "moodle_private_token": "...",
    "obtained_at": "2026-06-10T00:00:00Z",
    "expires_at": "2026-09-10T00:00:00Z"
  }
}
```

GCM authentication tag is appended to ciphertext by the crypto library.

### `user_devices`

```sql
CREATE TABLE user_devices (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    client_device_id TEXT NOT NULL,
    platform         TEXT NOT NULL,
    device_name      TEXT,
    app_version      TEXT,
    os_version       TEXT,
    last_seen_at     TIMESTAMPTZ,
    last_login_at    TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at       TIMESTAMPTZ,

    UNIQUE (user_id, client_device_id),

    CONSTRAINT chk_device_platform
        CHECK (platform IN ('ios', 'ipados', 'macos', 'windows', 'watchos', 'wearos', 'android'))
);
```

Anonymous devices continue using the existing `device_registrations` table.

### `auth_sessions`

```sql
CREATE TABLE auth_sessions (
    id                     BIGSERIAL PRIMARY KEY,
    user_id                UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_id              UUID REFERENCES user_devices(id) ON DELETE SET NULL,
    refresh_token_hash     TEXT NOT NULL UNIQUE,
    issued_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at             TIMESTAMPTZ NOT NULL,
    revoked_at             TIMESTAMPTZ,
    revoked_reason         TEXT,
    replaced_by_session_id BIGINT REFERENCES auth_sessions(id) ON DELETE SET NULL,
    reuse_detected_at      TIMESTAMPTZ,
    last_used_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_session_time CHECK (expires_at > issued_at),

    CONSTRAINT chk_revoked_reason
        CHECK (
            revoked_reason IS NULL OR
            revoked_reason IN (
                'logout', 'rotated', 'reuse_detected',
                'expired', 'admin_revoked', 'credential_revoked'
            )
        )
);

CREATE INDEX idx_auth_sessions_user_active
    ON auth_sessions (user_id) WHERE revoked_at IS NULL;
```

Access token: 15 min JWT (stateless). Refresh token: 90 days, stored as SHA-256 hash.

Refresh rotation: on use, old session gets `revoked_at + replaced_by_session_id`, new session created. If a revoked token is reused, `reuse_detected_at` is set and all sessions for that device are revoked.

---

## 2. Push Tokens & Push Jobs

### `device_push_tokens`

```sql
CREATE TABLE device_push_tokens (
    id                BIGSERIAL PRIMARY KEY,
    device_id         UUID NOT NULL REFERENCES user_devices(id) ON DELETE CASCADE,
    provider          TEXT NOT NULL,
    token_kind        TEXT NOT NULL,
    token_hash        TEXT NOT NULL,
    token_value       TEXT NOT NULL,
    bundle_id         TEXT,
    topic             TEXT,
    environment       TEXT,
    scope_key         TEXT NOT NULL DEFAULT '',
    expires_at        TIMESTAMPTZ,
    status            TEXT NOT NULL DEFAULT 'active',
    last_success_at   TIMESTAMPTZ,
    last_failure_at   TIMESTAMPTZ,
    last_failure_code TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_push_token_provider
        CHECK (provider IN ('apns', 'fcm')),
    CONSTRAINT chk_push_token_kind
        CHECK (token_kind IN ('standard', 'push_to_start', 'live_activity_update')),
    CONSTRAINT chk_push_token_status
        CHECK (status IN ('active', 'invalidated', 'expired')),
    CONSTRAINT chk_push_token_env
        CHECK (environment IS NULL OR environment IN ('development', 'production'))
);

CREATE UNIQUE INDEX ux_push_token_active
    ON device_push_tokens (provider, token_kind, token_hash, scope_key)
    WHERE status = 'active';

CREATE INDEX idx_push_tokens_device_active
    ON device_push_tokens (device_id, token_kind, status);

CREATE INDEX idx_push_tokens_expiry
    ON device_push_tokens (status, expires_at)
    WHERE status = 'active';
```

Token kinds:

| token_kind | Purpose | Lifetime |
|---|---|---|
| `standard` | Regular push (assignment reminders, bulletins) | Long-lived |
| `push_to_start` | iOS Push-to-Start Live Activity | Medium, refreshed on app restart |
| `live_activity_update` | Update an active Live Activity | Short, ends with activity |

`scope_key`: identifies what a token is bound to (e.g., `assignment:12345` for a Live Activity token). Empty string for standard/PTS tokens.

`token_hash`: SHA-256 of the raw token, used for uniqueness lookups. `token_value`: raw token used for actual push delivery.

### `push_jobs`

One row = one logical notification to a user. Does not track per-device delivery.

```sql
CREATE TABLE push_jobs (
    id           BIGSERIAL PRIMARY KEY,
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_id    UUID REFERENCES user_devices(id) ON DELETE SET NULL,
    dedupe_key   TEXT NOT NULL,
    channel      TEXT NOT NULL,
    scenario     TEXT NOT NULL,
    fire_at      TIMESTAMPTZ NOT NULL,
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    priority     INTEGER NOT NULL DEFAULT 100,
    payload      JSONB NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    attempts     INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    locked_at    TIMESTAMPTZ,
    locked_by    TEXT,
    last_error   TEXT,
    sent_at      TIMESTAMPTZ,
    cancelled_at TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_push_job_channel
        CHECK (channel IN ('assignment', 'course', 'bulletin', 'system', 'custom')),
    CONSTRAINT chk_push_job_status
        CHECK (status IN ('pending', 'processing', 'sent', 'partial_failed', 'failed', 'cancelled')),
    CONSTRAINT chk_push_job_attempts
        CHECK (attempts >= 0 AND max_attempts > 0)
);

CREATE UNIQUE INDEX ux_push_jobs_dedupe_active
    ON push_jobs (user_id, dedupe_key)
    WHERE status IN ('pending', 'processing');

CREATE INDEX idx_push_jobs_due
    ON push_jobs (status, fire_at, available_at, priority)
    WHERE status = 'pending';
```

`device_id = NULL` means fan-out to all active devices for the user.

Dedupe key format: `{channel}:{entity_type}:{entity_id}:{scenario}`

Examples:

```
assignment:moodle:98765:reminder_24h
course:user_course:123:start_10m
bulletin:bulletin:456:new_bulletin
system:device:abc:reauth_required
```

### `push_deliveries`

One row = one push_job sent to one specific token. Tracks per-token delivery result.

```sql
CREATE TABLE push_deliveries (
    id                  BIGSERIAL PRIMARY KEY,
    push_job_id         BIGINT NOT NULL REFERENCES push_jobs(id) ON DELETE CASCADE,
    user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_id           UUID REFERENCES user_devices(id) ON DELETE SET NULL,
    push_token_id       BIGINT REFERENCES device_push_tokens(id) ON DELETE SET NULL,
    provider            TEXT NOT NULL,
    token_kind          TEXT NOT NULL,
    token_hash          TEXT NOT NULL,
    scope_key           TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'pending',
    attempts            INTEGER NOT NULL DEFAULT 0,
    max_attempts        INTEGER NOT NULL DEFAULT 3,
    provider_message_id TEXT,
    failure_code        TEXT,
    failure_message     TEXT,
    sent_at             TIMESTAMPTZ,
    next_retry_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_delivery_status
        CHECK (status IN ('pending', 'sent', 'failed', 'skipped')),
    CONSTRAINT chk_delivery_provider
        CHECK (provider IN ('apns', 'fcm')),
    CONSTRAINT chk_delivery_token_kind
        CHECK (token_kind IN ('standard', 'push_to_start', 'live_activity_update')),
    CONSTRAINT chk_delivery_attempts
        CHECK (attempts >= 0 AND max_attempts > 0)
);

CREATE UNIQUE INDEX ux_push_delivery_job_token
    ON push_deliveries (push_job_id, token_hash, token_kind, scope_key);

CREATE INDEX idx_push_deliveries_pending
    ON push_deliveries (status, next_retry_at)
    WHERE status = 'pending';
```

Denormalized `provider`, `token_kind`, `token_hash` preserve history even if the token row is later deleted.

---

## 3. Academic Data

### `user_courses`

```sql
CREATE TABLE user_courses (
    id                BIGSERIAL PRIMARY KEY,
    user_id           UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    semester          TEXT NOT NULL,
    course_key        TEXT NOT NULL,
    course_no         TEXT,
    source            TEXT NOT NULL DEFAULT 'ntust_portal',
    course_name       TEXT NOT NULL,
    course_name_en    TEXT,
    instructors       TEXT[] NOT NULL DEFAULT '{}',
    credits           NUMERIC(3,1),
    classroom         TEXT,
    enrolled_count    SMALLINT,
    max_count         SMALLINT,
    moodle_id         TEXT,
    schedule_json     JSONB NOT NULL DEFAULT '[]',
    classroom_map     JSONB NOT NULL DEFAULT '{}',
    raw_payload       JSONB,
    enrollment_status TEXT NOT NULL DEFAULT 'enrolled',
    fetched_at        TIMESTAMPTZ,
    first_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at      TIMESTAMPTZ,
    deleted_at        TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (user_id, semester, course_key),

    CONSTRAINT chk_course_source
        CHECK (source IN ('ntust_portal', 'user_added')),
    CONSTRAINT chk_course_source_key
        CHECK (
            (source = 'ntust_portal' AND course_no IS NOT NULL)
            OR source = 'user_added'
        ),
    CONSTRAINT chk_enrollment_status
        CHECK (enrollment_status IN ('enrolled', 'dropped', 'completed'))
);

CREATE INDEX idx_user_courses_semester
    ON user_courses (user_id, semester)
    WHERE deleted_at IS NULL;
```

`course_key`: stable identifier for upsert.
- `source = ntust_portal`: `course_key = course_no`
- `source = user_added`: `course_key = 'manual:' || uuid`

### `user_course_overrides`

Per-field timestamps for merge-based sync.

```sql
CREATE TABLE user_course_overrides (
    id                     BIGSERIAL PRIMARY KEY,
    user_id                UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    user_course_id         BIGINT NOT NULL REFERENCES user_courses(id) ON DELETE CASCADE,

    custom_name            TEXT,
    custom_name_updated_at TIMESTAMPTZ,
    custom_name_device_id  UUID REFERENCES user_devices(id) ON DELETE SET NULL,

    color_hex              TEXT,
    color_hex_updated_at   TIMESTAMPTZ,
    color_hex_device_id    UUID REFERENCES user_devices(id) ON DELETE SET NULL,

    is_hidden              BOOLEAN NOT NULL DEFAULT false,
    is_hidden_updated_at   TIMESTAMPTZ,
    is_hidden_device_id    UUID REFERENCES user_devices(id) ON DELETE SET NULL,

    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (user_id, user_course_id)
);
```

Merge sync example: iPhone changes `color_hex` (T1), iPad changes `custom_name` (T2). On sync, each field's timestamp is compared independently — both changes are preserved.

### `user_course_skipped_dates`

Each skipped date is an independent entity for correct merge sync.

```sql
CREATE TABLE user_course_skipped_dates (
    id                   BIGSERIAL PRIMARY KEY,
    user_id              UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    user_course_id       BIGINT NOT NULL REFERENCES user_courses(id) ON DELETE CASCADE,
    skipped_on           DATE NOT NULL,
    reason               TEXT,
    created_by_device_id UUID REFERENCES user_devices(id) ON DELETE SET NULL,
    deleted_at           TIMESTAMPTZ,
    deleted_by_device_id UUID REFERENCES user_devices(id) ON DELETE SET NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (user_id, user_course_id, skipped_on)
);

CREATE INDEX idx_course_skipped_dates_active
    ON user_course_skipped_dates (user_id, skipped_on)
    WHERE deleted_at IS NULL;
```

### `user_assignments`

```sql
CREATE TABLE user_assignments (
    id                      BIGSERIAL PRIMARY KEY,
    user_id                 UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    user_course_id          BIGINT REFERENCES user_courses(id) ON DELETE SET NULL,
    moodle_course_id        BIGINT NOT NULL,
    moodle_assignment_id    BIGINT NOT NULL,
    course_no               TEXT,
    course_name             TEXT,
    title                   TEXT NOT NULL,
    due_at                  TIMESTAMPTZ,
    cutoff_at               TIMESTAMPTZ,
    allow_from_at           TIMESTAMPTZ,
    moodle_url              TEXT,
    intro_html              TEXT,
    provider_is_submitted   BOOLEAN NOT NULL DEFAULT false,
    provider_submitted_at   TIMESTAMPTZ,
    provider_grading_status TEXT,
    provider_grade          TEXT,
    raw_payload             JSONB,
    fetched_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    first_seen_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at              TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (user_id, moodle_course_id, moodle_assignment_id)
);

CREATE INDEX idx_user_assignments_active
    ON user_assignments (user_id)
    WHERE deleted_at IS NULL;

CREATE INDEX idx_user_assignments_due
    ON user_assignments (user_id, due_at)
    WHERE deleted_at IS NULL AND provider_is_submitted = false;

CREATE INDEX idx_user_assignments_course
    ON user_assignments (user_course_id)
    WHERE deleted_at IS NULL;
```

`course_no` and `course_name` are snapshot fields — preserved even if `user_course_id` mapping fails.

### `user_assignment_overrides`

```sql
CREATE TABLE user_assignment_overrides (
    id                       BIGSERIAL PRIMARY KEY,
    user_id                  UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    user_assignment_id       BIGINT NOT NULL REFERENCES user_assignments(id) ON DELETE CASCADE,

    local_status             TEXT NOT NULL DEFAULT 'none',
    local_status_updated_at  TIMESTAMPTZ,
    local_status_device_id   UUID REFERENCES user_devices(id) ON DELETE SET NULL,

    note                     TEXT,
    note_updated_at          TIMESTAMPTZ,
    note_device_id           UUID REFERENCES user_devices(id) ON DELETE SET NULL,

    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (user_id, user_assignment_id),

    CONSTRAINT chk_local_status
        CHECK (local_status IN ('none', 'locally_completed', 'ignored', 'archived'))
);
```

Status semantics:

| Status | Excludes from reminders | Hidden from main list |
|---|---|---|
| `none` | No | No |
| `locally_completed` | Yes | Optional |
| `ignored` | Yes | Yes |
| `archived` | Yes | Yes |

Reminder exclusion query:

```sql
provider_is_submitted = true
OR local_status IN ('locally_completed', 'ignored', 'archived')
```

---

## 4. Settings & Preferences

### `user_settings_documents`

```sql
CREATE TABLE user_settings_documents (
    id                   BIGSERIAL PRIMARY KEY,
    user_id              UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    namespace            TEXT NOT NULL,
    schema_version       INTEGER NOT NULL DEFAULT 1,
    document             JSONB NOT NULL,
    revision             BIGINT NOT NULL DEFAULT 1,
    created_by_device_id UUID REFERENCES user_devices(id) ON DELETE SET NULL,
    updated_by_device_id UUID REFERENCES user_devices(id) ON DELETE SET NULL,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at           TIMESTAMPTZ,

    CONSTRAINT chk_settings_namespace
        CHECK (namespace IN (
            'home_layout', 'appearance', 'assignment_display',
            'notification', 'browser', 'language',
            'schedule_display', 'watch', 'wearos'
        )),
    CONSTRAINT chk_settings_revision CHECK (revision >= 1),
    CONSTRAINT chk_settings_schema_version CHECK (schema_version >= 1)
);

CREATE UNIQUE INDEX ux_user_settings_namespace_active
    ON user_settings_documents (user_id, namespace)
    WHERE deleted_at IS NULL;

CREATE INDEX idx_user_settings_user_updated
    ON user_settings_documents (user_id, updated_at)
    WHERE deleted_at IS NULL;
```

`schema_version` lives only in the table column, not inside the JSONB document.

Revision is server-generated, monotonically incremented on every successful write. Clients cannot set revision directly — they submit `base_revision` for optimistic concurrency.

Atomic update:

```sql
UPDATE user_settings_documents
SET document = :document,
    schema_version = :schema_version,
    revision = revision + 1,
    updated_by_device_id = :device_id,
    updated_at = now()
WHERE user_id = :user_id
  AND namespace = :namespace
  AND revision = :base_revision
  AND deleted_at IS NULL
RETURNING revision;
```

No rows returned = conflict.

Namespaces:

| namespace | Content |
|---|---|
| `home_layout` | Home screen sections, sort order, visibility, widgets |
| `appearance` | visual_preset, accent_color, theme |
| `assignment_display` | Default filter, sort order |
| `notification` | Reminders, Live Activity toggles, offsets (grouped by domain internally) |
| `browser` | Link opening behavior, Moodle open target |
| `language` | App language, course/classroom abbreviation settings |
| `schedule_display` | Timetable display preferences |
| `watch` | watchOS-specific settings |
| `wearos` | Wear OS-specific settings |

`notification` document structure (grouped by domain):

```json
{
  "assignments": {
    "enabled": true,
    "reminder_offsets_hours": [48, 24, 8, 2, 1, 0.5]
  },
  "courses": {
    "enabled": true,
    "start_lead_minutes": 10
  },
  "bulletins": {
    "enabled": true
  },
  "live_activities": {
    "enabled": true,
    "scenarios": ["classPreparingCountdown", "inClass", "assignmentCountdown"]
  }
}
```

---

## 5. Bulletins

The existing `bulletins` table is unchanged. Changes below support user-level subscriptions and cross-device read/starred/hidden state.

### `user_bulletin_subscriptions`

```sql
CREATE TABLE user_bulletin_subscriptions (
    id                   BIGSERIAL PRIMARY KEY,
    user_id              UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name                 TEXT,
    orgs                 TEXT[] NOT NULL DEFAULT '{}',
    tags                 TEXT[] NOT NULL DEFAULT '{}',
    mode                 TEXT NOT NULL DEFAULT 'AND',
    enabled              BOOLEAN NOT NULL DEFAULT true,
    revision             BIGINT NOT NULL DEFAULT 1,
    created_by_device_id UUID REFERENCES user_devices(id) ON DELETE SET NULL,
    updated_by_device_id UUID REFERENCES user_devices(id) ON DELETE SET NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at           TIMESTAMPTZ,

    CONSTRAINT chk_subscription_mode CHECK (mode IN ('AND', 'OR')),
    CONSTRAINT chk_subscription_revision CHECK (revision >= 1)
);

CREATE INDEX idx_bulletin_subs_user_active
    ON user_bulletin_subscriptions (user_id)
    WHERE enabled = true AND deleted_at IS NULL;
```

API: per-rule CRUD (`POST`, `PATCH /{id}`, `DELETE /{id}`), not bulk PUT.

Matcher wildcard rules:
- `orgs = {}` means no org filter (matches all orgs)
- `tags = {}` means no tag filter (matches all tags)
- `mode = AND`: both org_match AND tag_match must pass (wildcards always pass)
- `mode = OR`: org_match OR tag_match must pass. If one side is wildcard (empty), only the non-empty side participates. Both empty = match all.

### `user_bulletin_states`

Per-field boolean + updated_at for merge sync (supports both set and unset operations).

```sql
CREATE TABLE user_bulletin_states (
    id                  BIGSERIAL PRIMARY KEY,
    user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    bulletin_id         BIGINT NOT NULL REFERENCES bulletins(id) ON DELETE CASCADE,

    is_read             BOOLEAN NOT NULL DEFAULT false,
    read_updated_at     TIMESTAMPTZ,
    read_device_id      UUID REFERENCES user_devices(id) ON DELETE SET NULL,
    first_read_at       TIMESTAMPTZ,

    is_starred          BOOLEAN NOT NULL DEFAULT false,
    starred_updated_at  TIMESTAMPTZ,
    starred_device_id   UUID REFERENCES user_devices(id) ON DELETE SET NULL,

    is_hidden           BOOLEAN NOT NULL DEFAULT false,
    hidden_updated_at   TIMESTAMPTZ,
    hidden_device_id    UUID REFERENCES user_devices(id) ON DELETE SET NULL,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (user_id, bulletin_id)
);

CREATE INDEX idx_bulletin_states_starred
    ON user_bulletin_states (user_id, starred_updated_at)
    WHERE is_starred = true;

CREATE INDEX idx_bulletin_states_hidden
    ON user_bulletin_states (user_id, hidden_updated_at)
    WHERE is_hidden = true;
```

Unread query uses LEFT JOIN from `bulletin_user_matches`:

```sql
SELECT b.*
FROM bulletin_user_matches m
JOIN bulletins b ON b.id = m.bulletin_id
LEFT JOIN user_bulletin_states s
  ON s.user_id = m.user_id AND s.bulletin_id = m.bulletin_id
WHERE m.user_id = :user_id
  AND (s.is_read IS NULL OR s.is_read = false)
  AND (s.is_hidden IS NULL OR s.is_hidden = false);
```

### `bulletin_user_matches`

```sql
CREATE TABLE bulletin_user_matches (
    id              BIGSERIAL PRIMARY KEY,
    bulletin_id     BIGINT NOT NULL REFERENCES bulletins(id) ON DELETE CASCADE,
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    subscription_id BIGINT REFERENCES user_bulletin_subscriptions(id) ON DELETE SET NULL,
    match_reason    JSONB,
    push_job_id     BIGINT REFERENCES push_jobs(id) ON DELETE SET NULL,
    pushed_at       TIMESTAMPTZ,
    matched_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (bulletin_id, user_id)
);

CREATE INDEX idx_bulletin_matches_user
    ON bulletin_user_matches (user_id, matched_at DESC);

CREATE INDEX idx_bulletin_matches_pending_push
    ON bulletin_user_matches (pushed_at)
    WHERE pushed_at IS NULL;
```

`subscription_id` records one of the matching rules (not necessarily all). `match_reason` can store additional match info if needed.

---

## 6. Sync Infrastructure

### `sync_policies`

Global configuration for server-side sync job scheduling. Managed via admin dashboard.

```sql
CREATE TABLE sync_policies (
    id                       BIGSERIAL PRIMARY KEY,
    job_type                 TEXT NOT NULL UNIQUE,
    enabled                  BOOLEAN NOT NULL DEFAULT true,
    default_interval_seconds INTEGER NOT NULL,
    active_from              TIMESTAMPTZ,
    active_until             TIMESTAMPTZ,
    priority                 INTEGER NOT NULL DEFAULT 100,
    max_attempts             INTEGER NOT NULL DEFAULT 3,
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_sync_policy_job_type
        CHECK (job_type IN (
            'ntust_courses', 'moodle_assignments', 'calendar', 'grades'
        )),
    CONSTRAINT chk_sync_policy_interval CHECK (default_interval_seconds > 0),
    CONSTRAINT chk_sync_policy_attempts CHECK (max_attempts > 0)
);
```

Default seed values:

| job_type | interval | enabled | notes |
|---|---|---|---|
| `moodle_assignments` | 28800 (8h) | true | Also triggered before push notifications |
| `ntust_courses` | 28800 (8h) | false | Enable during course selection/dropout periods via dashboard |
| `calendar` | 604800 (7d) | true | Admin can trigger manually |
| `grades` | 28800 (8h) | false | Enable when implemented |

### `sync_jobs`

One row per user per job type. Recurring: on success, returns to `pending` with updated `run_after`.

```sql
CREATE TABLE sync_jobs (
    id                  BIGSERIAL PRIMARY KEY,
    user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    external_account_id BIGINT NOT NULL REFERENCES external_accounts(id) ON DELETE CASCADE,
    job_type            TEXT NOT NULL,
    priority            INTEGER NOT NULL DEFAULT 100,
    status              TEXT NOT NULL DEFAULT 'pending',
    run_after           TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_by           TEXT,
    locked_at           TIMESTAMPTZ,
    attempts            INTEGER NOT NULL DEFAULT 0,
    max_attempts        INTEGER NOT NULL DEFAULT 3,
    last_success_at     TIMESTAMPTZ,
    last_failure_at     TIMESTAMPTZ,
    last_error          TEXT,
    cursor              JSONB NOT NULL DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (user_id, job_type),

    CONSTRAINT chk_sync_job_type
        CHECK (job_type IN (
            'ntust_courses', 'moodle_assignments', 'calendar', 'grades'
        )),
    CONSTRAINT chk_sync_job_status
        CHECK (status IN ('pending', 'running', 'failed', 'disabled')),
    CONSTRAINT chk_sync_job_attempts
        CHECK (attempts >= 0 AND max_attempts > 0)
);

CREATE INDEX idx_sync_jobs_due
    ON sync_jobs (status, run_after, priority)
    WHERE status = 'pending';
```

Status lifecycle:
- `pending → running → pending` (success, with `run_after = now() + interval`)
- `pending → running → pending` (retriable failure, with backoff)
- `pending → running → failed` (max attempts exceeded)
- Any → `disabled` (credentials invalid, user must reauthorize)

Stale lock recovery: jobs with `status = 'running' AND locked_at < now() - INTERVAL '10 minutes'` are reset to `pending` by a periodic recovery sweep.

### `sync_runs`

```sql
CREATE TABLE sync_runs (
    id          BIGSERIAL PRIMARY KEY,
    sync_job_id BIGINT NOT NULL REFERENCES sync_jobs(id) ON DELETE CASCADE,
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    status      TEXT NOT NULL DEFAULT 'running',
    fetched_count INTEGER,
    changed_count INTEGER,
    error       TEXT,
    metadata    JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_sync_run_status
        CHECK (status IN ('running', 'succeeded', 'failed', 'cancelled'))
);

CREATE INDEX idx_sync_runs_job
    ON sync_runs (sync_job_id, started_at DESC);

CREATE INDEX idx_sync_runs_user_recent
    ON sync_runs (user_id, started_at DESC);
```

### `user_sync_state`

```sql
CREATE TABLE user_sync_state (
    user_id            UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    current_revision   BIGINT NOT NULL DEFAULT 0,
    compacted_revision BIGINT NOT NULL DEFAULT 0,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_user_sync_revision
        CHECK (current_revision >= compacted_revision)
);
```

`current_revision`: updated on every change log append.
`compacted_revision`: updated when old changelog entries are purged. Client with `since_revision < compacted_revision` must do a full sync.

### `user_change_log`

```sql
CREATE TABLE user_change_log (
    revision    BIGSERIAL PRIMARY KEY,
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    operation   TEXT NOT NULL,
    payload     JSONB,
    device_id   UUID REFERENCES user_devices(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_changelog_operation
        CHECK (operation IN ('upsert', 'delete')),
    CONSTRAINT chk_changelog_entity_type
        CHECK (entity_type IN (
            'course', 'course_override', 'course_skipped_date',
            'assignment', 'assignment_override',
            'settings_document',
            'bulletin_subscription', 'bulletin_state', 'bulletin_match'
        ))
);

CREATE INDEX idx_change_log_user_revision
    ON user_change_log (user_id, revision);
```

Revision is a global BIGSERIAL, not per-user sequential. Gaps are normal.

Entity ID format:

| entity_type | entity_id |
|---|---|
| `course` | `{user_course_id}` |
| `course_override` | `{user_course_id}` |
| `course_skipped_date` | `{skipped_date_id}` |
| `assignment` | `{user_assignment_id}` |
| `assignment_override` | `{user_assignment_id}` |
| `settings_document` | `{namespace}` |
| `bulletin_subscription` | `{subscription_id}` |
| `bulletin_state` | `{bulletin_id}` |
| `bulletin_match` | `{bulletin_id}` |

What writes changelog:
- ALL user-scoped data changes visible to the client, including server-side academic data updates
- Server-side sync updates to `user_courses` and `user_assignments` write changelog entries so other devices know to refresh

What does NOT write changelog:
- `push_jobs`, `push_deliveries`, `sync_jobs`, `sync_runs`, `auth_sessions`, `device_push_tokens`

Payload contains routing hints only — never full documents, sensitive data, or HTML content.

Retention: entries older than 30 days are purged. `compacted_revision` is updated per user to the max deleted revision. Clients with `since_revision < compacted_revision` receive HTTP 410 and must do a full sync.
