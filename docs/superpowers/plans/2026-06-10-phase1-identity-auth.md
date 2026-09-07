# Phase 1: Identity, Device & Auth Foundation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement migration-plan Phase 1 — `users` / `external_accounts` / `external_account_credentials` / `auth_sessions` / `user_devices` / `device_push_tokens` / `push_jobs` / `push_deliveries` tables plus `/v3` auth and device endpoints, incorporating the security-review fixes (rate limiting, unverified-password flag, refresh-reuse grace window, soft-delete reactivation, device-deletion session revocation).

**Architecture:** New `server/auth/` package (models, crypto, tokens, rate limiting, Moodle verification, service) + two new route modules mounted under a new `/v3` prefix. Existing `/v2` code is untouched. Tables are added via one Alembic migration; ORM models follow the existing `server/models.py` style (plain string status columns + StrEnum, partial indexes, TIMESTAMPTZ).

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2 async, PostgreSQL 17, Alembic, PyJWT (HS256), `cryptography` AESGCM (already present via `pyjwt[crypto]`), pytest + pytest-asyncio (Postgres-backed conftest).

**Security-review fixes folded in:**
- 1.3 — in-memory sliding-window rate limit on login (per student_id and per client IP), 429 on excess.
- 1.4 — credential blob stores `password_verified: false`; never auto-retried against SSO in Phase 1.
- 1.5 — refresh rotation with 60 s grace window for benign retries; reuse outside grace revokes the whole token family (follows `replaced_by_session_id` chain, not nullable `device_id`).
- 1.7 — re-login with the student_id of a soft-deleted user reactivates that user.
- Device deletion revokes the device's sessions and invalidates its push tokens.

**Out of scope (later phases):** sync tables/APIs, server-side school sync, push pipeline activation (push_jobs/push_deliveries are created but unused), account deletion endpoint, dual-write to `device_registrations` (frontend keeps calling `/v2/devices/register` itself).

---

## File Structure

```
server/
├── auth/
│   ├── __init__.py          # empty package marker
│   ├── models.py            # 8 ORM models (Phase 1 tables)
│   ├── crypto.py            # CredentialCipher (AES-256-GCM envelope encryption)
│   ├── tokens.py            # JWT access tokens + refresh token generate/hash
│   ├── rate_limit.py        # SlidingWindowLimiter (in-memory)
│   ├── moodle.py            # MoodleVerifier protocol + Http/Static impls
│   ├── service.py           # login / refresh / logout business logic
│   └── dependencies.py      # CurrentAuth FastAPI dependency (Bearer JWT)
├── routes/
│   ├── auth.py              # POST /auth/login|refresh|logout
│   └── user_devices.py      # /devices (v3): register, list, delete
├── migrations/versions/
│   └── a7c3f9e1d2b8_phase1_identity_auth.py
├── config.py                # + auth/crypto/moodle settings
├── main.py                  # + _mount_api_v3()
└── tests/
    ├── conftest.py          # + auth secrets in test_settings
    ├── test_auth_crypto.py
    ├── test_auth_tokens.py
    ├── test_auth_rate_limit.py
    ├── test_auth_login.py
    ├── test_auth_refresh.py
    └── test_user_devices_v3.py
```

Commit format (user preference): `feat(Scope): short description` + bullet body, no Co-Authored-By.

---

### Task 1: Auth settings

**Files:** Modify `server/config.py`, `server/tests/conftest.py`

- [ ] Add to `Settings` (after the `api_shared_secret` block):

```python
    # --- v3 user accounts / auth ---
    api_v3_base_path: str = "/v3"
    # HS256 signing key for access JWTs. Empty disables /v3 auth (login
    # returns 503) — mirrors the api_shared_secret dev convention.
    auth_jwt_secret: str = ""
    # Separate HMAC-SHA-256 key for refresh token hashing (key separation).
    auth_refresh_hmac_key: str = ""
    auth_access_token_ttl_seconds: int = 900          # 15 min
    auth_refresh_token_ttl_days: int = 90
    # Benign-retry window after refresh rotation (review fix 1.5).
    auth_refresh_reuse_grace_seconds: int = 60
    # Login rate limit (review fix 1.3): N attempts per window, per key.
    auth_login_max_attempts: int = 5
    auth_login_window_seconds: int = 900

    # --- Credential envelope encryption (review: key rotation via key_id) ---
    # JSON map of key_id -> base64-encoded 32-byte AES key, e.g.
    # TIGERDUCK_CREDENTIAL_KEYS='{"v1":"<base64>"}'
    credential_keys: dict[str, str] = Field(default_factory=dict)
    credential_active_key_id: str = ""

    # --- Moodle token verification ---
    moodle_base_url: str = "https://moodle.ntust.edu.tw"
    moodle_verify_timeout_seconds: float = 10.0
```

- [ ] In `conftest.py` `test_settings`, add: `auth_jwt_secret="test-jwt-secret"`, `auth_refresh_hmac_key="test-hmac-key"`, `credential_keys={"v1": base64.b64encode(b"0"*32).decode()}`, `credential_active_key_id="v1"` (import `base64`).
- [ ] Run `uv run pytest server/tests/test_schemas_device_register.py -q` (settings smoke) → PASS
- [ ] Commit: `feat(Auth): add v3 auth and credential encryption settings`

### Task 2: ORM models for the 8 Phase-1 tables

**Files:** Create `server/auth/__init__.py`, `server/auth/models.py`; Test `server/tests/test_auth_models.py`

Models follow `server/models.py` conventions (StrEnum + String columns, TIMESTAMPTZ, partial indexes). Tables, matching the data-model spec DDL:

- `User` (`users`): UUID pk `gen_random_uuid()`, `student_id`, `display_name`, `status` (active/suspended/pending_deletion), `locale` default `zh-Hant`, `last_login_at`, timestamps, `deleted_at`. Partial unique index `ux_users_student_id_active` on `student_id` WHERE `student_id IS NOT NULL AND deleted_at IS NULL`.
- `ExternalAccount` (`external_accounts`): BIGSERIAL pk, FK user CASCADE, `provider`, `external_user_id`, `credential_status` (active/expired/invalid/revoked), auth success/failure timestamps + `last_auth_error`, UNIQUE(provider, external_user_id), UNIQUE(user_id, provider).
- `ExternalAccountCredential` (`external_account_credentials`): pk = FK external_account CASCADE, `encryption_algorithm` default AES-256-GCM, `encryption_key_id`, `ciphertext` BYTEA, `nonce` BYTEA, `aad`, `version`, `rotated_at`, `last_used_at`, timestamps.
- `UserDevice` (`user_devices`): UUID pk, FK user CASCADE, `client_device_id`, `platform` (ios/ipados/macos/windows/watchos/wearos/android), `device_name`, `app_version`, `os_version`, `last_seen_at`, `last_login_at`, timestamps, `deleted_at`, UNIQUE(user_id, client_device_id).
- `AuthSession` (`auth_sessions`): BIGSERIAL pk, FK user CASCADE, FK device SET NULL, `refresh_token_hash` UNIQUE, `issued_at`, `expires_at`, `revoked_at`, `revoked_reason` (logout/rotated/reuse_detected/expired/admin_revoked/credential_revoked), self-FK `replaced_by_session_id` SET NULL, `reuse_detected_at`, `last_used_at`. Partial index `idx_auth_sessions_user_active` on user_id WHERE revoked_at IS NULL.
- `DevicePushToken` (`device_push_tokens`): BIGSERIAL pk, FK device CASCADE, `provider` (apns/fcm), `token_kind` (standard/push_to_start/live_activity_update), `token_hash`, `token_value`, `bundle_id`, `topic`, `environment`, `scope_key` default '', `expires_at`, `status` (active/invalidated/expired), failure/success tracking, timestamps. Partial unique `ux_push_token_active` (provider, token_kind, token_hash, scope_key) WHERE status='active'; indexes per spec.
- `PushJob` (`push_jobs`) and `PushDelivery` (`push_deliveries`): per spec DDL, created Phase 1, unused until Phase 4. **Review fix 1.2**: the dedupe partial unique index covers `status IN ('pending','processing','sent','partial_failed')` (not just pending/processing) so sent reminders cannot be re-created by the next sync round.

- [ ] Write `test_auth_models.py`: uses `db_session` fixture; asserts (a) creating a `User` + `ExternalAccount` + `ExternalAccountCredential` round-trips, (b) `ux_users_student_id_active` allows same student_id when the older row is soft-deleted, (c) UNIQUE(provider, external_user_id) raises IntegrityError on duplicate.
- [ ] Run → FAIL (module missing)
- [ ] Implement `server/auth/models.py`; import it from `server/models.py` bottom (`from server.auth.models import *  # noqa` — re-export so `Base.metadata` sees the tables) — actually import in `server/auth/__init__.py` is not enough; conftest's `create_all` uses `Base.metadata`, which picks up models on import of `server.auth.models`. Add `import server.auth.models  # noqa: F401` to `server/models.py`.
- [ ] Run → PASS
- [ ] Commit: `feat(Auth): add ORM models for Phase 1 identity and push tables`

### Task 3: Alembic migration

**Files:** Create `server/migrations/versions/a7c3f9e1d2b8_phase1_identity_auth.py` (down_revision = `e2a1b7c4d9f3`)

- [ ] `upgrade()`: create the 8 tables + all indexes/constraints in FK order (users → external_accounts → external_account_credentials / user_devices → auth_sessions / device_push_tokens → push_jobs → push_deliveries). Use `sa.text("gen_random_uuid()")` server defaults; CHECK constraints as in spec.
- [ ] `downgrade()`: drop in reverse order.
- [ ] Verify chain: `uv run alembic history | head` shows new head.
- [ ] Commit: `feat(Auth): add alembic migration for Phase 1 tables`

### Task 4: Credential envelope encryption

**Files:** Create `server/auth/crypto.py`; Test `server/tests/test_auth_crypto.py`

API:

```python
@dataclass(frozen=True)
class EncryptedBlob:
    key_id: str
    nonce: bytes        # 12 bytes
    ciphertext: bytes   # includes GCM tag (AESGCM appends it)
    aad: str

class CredentialCipherError(Exception): ...

class CredentialCipher:
    def __init__(self, keys: dict[str, str], active_key_id: str) -> None:
        # keys: key_id -> base64 32-byte key; validates active_key_id present
    @classmethod
    def from_settings(cls, settings) -> "CredentialCipher": ...
    def encrypt(self, payload: dict, aad: str) -> EncryptedBlob:
        # json.dumps -> AESGCM(key).encrypt(nonce=os.urandom(12), data, aad)
    def decrypt(self, blob: EncryptedBlob) -> dict:
        # uses blob.key_id (supports old keys during rotation); raises
        # CredentialCipherError on unknown key / InvalidTag

def build_credential_aad(external_account_id: int, provider: str) -> str:
    return f"external_account:{external_account_id}:provider:{provider}"
```

- [ ] Tests: round-trip; decrypt with tampered ciphertext raises; decrypt with wrong AAD raises; decrypt picks non-active key by key_id; unknown key_id raises; missing active key on init raises.
- [ ] RED → implement → GREEN
- [ ] Commit: `feat(Auth): add AES-256-GCM credential envelope encryption`

### Task 5: Token primitives

**Files:** Create `server/auth/tokens.py`; Test `server/tests/test_auth_tokens.py`

```python
@dataclass(frozen=True)
class AccessClaims:
    user_id: str
    session_id: int
    device_id: str | None

class InvalidAccessToken(Exception): ...

def issue_access_token(secret, *, user_id, session_id, device_id, ttl_seconds, now=None) -> str
    # HS256; claims: sub, sid, did, iat, exp, token_use="access"
def decode_access_token(secret, token) -> AccessClaims
    # verifies signature+exp, requires token_use=="access"
def generate_refresh_token() -> str          # secrets.token_urlsafe(48)
def hash_refresh_token(hmac_key: str, token: str) -> str   # hex HMAC-SHA-256
```

- [ ] Tests: round-trip claims; expired token raises; wrong secret raises; token without `token_use` raises; refresh hash is deterministic, 64 hex chars, differs across keys.
- [ ] RED → implement → GREEN
- [ ] Commit: `feat(Auth): add JWT access token and refresh token primitives`

### Task 6: Login rate limiter

**Files:** Create `server/auth/rate_limit.py`; Test `server/tests/test_auth_rate_limit.py`

```python
class SlidingWindowLimiter:
    def __init__(self, max_attempts: int, window_seconds: float, clock=time.monotonic)
    def allow(self, key: str) -> bool      # True if under the limit
    def record(self, key: str) -> None     # record an attempt
    def reset(self, key: str) -> None      # clear on success
```

In-memory, per-process (single-instance Docker Compose deployment; documented limitation). Prunes expired entries on access.

- [ ] Tests (injectable fake clock): allows up to N, blocks N+1, window expiry re-allows, reset clears, independent keys.
- [ ] RED → implement → GREEN
- [ ] Commit: `feat(Auth): add sliding-window login rate limiter`

### Task 7: Moodle token verifier

**Files:** Create `server/auth/moodle.py`; Test inline in `test_auth_login.py` (HTTP impl smoke-tested via `respx`-free manual stub — use a fake `httpx.AsyncClient` transport)

```python
@dataclass(frozen=True)
class MoodleVerifyResult:
    ok: bool
    username: str | None = None
    error: str | None = None    # "invalid_token" | "username_mismatch" | "unreachable"

class MoodleVerifier(Protocol):
    async def verify(self, *, token: str, student_id: str) -> MoodleVerifyResult

class HttpMoodleVerifier:
    # GET {base}/webservice/rest/server.php?wstoken=..&wsfunction=core_webservice_get_site_info&moodlewsrestformat=json
    # ok iff 200, no "exception" key, and username matches student_id case-insensitively

class StaticMoodleVerifier:
    # constructor takes the result to return; used by tests and dev
```

App wiring: `create_app` stores a `MoodleVerifier` on `app.state.moodle_verifier` (HttpMoodleVerifier by default); tests overwrite with StaticMoodleVerifier.

- [ ] Implement + unit test result mapping with a mocked transport (`httpx.MockTransport`).
- [ ] Commit: `feat(Auth): add Moodle token verifier with injectable protocol`

### Task 8: Login service + route

**Files:** Create `server/auth/service.py`, `server/auth/schemas.py`, `server/routes/auth.py`; Modify `server/main.py` (mount `/v3`); Test `server/tests/test_auth_login.py`

`POST /v3/auth/login` body: `{student_id, password, moodle_token, moodle_private_token?, device_info: {client_device_id, platform, device_name?, app_version?, os_version?}}`.

Flow (service `login()` — one DB transaction via SessionDep):
1. 503 if `auth_jwt_secret`/`auth_refresh_hmac_key`/credential keys unconfigured.
2. Rate limit (review 1.3): keys `sid:{student_id}` and `ip:{client_ip}`; if either disallowed → 429. `record()` both **before** calling Moodle (login attempts count even when verification errors out).
3. Verify Moodle token via `app.state.moodle_verifier`; on `ok=False` → 401 `{"detail": "moodle_verification_failed"}`.
4. Find `external_accounts` by (provider='ntust_sso', external_user_id=student_id):
   - found + user soft-deleted → reactivate user (`deleted_at=None`, `status='active'`) (review 1.7)
   - not found → create `User` (student_id, status active) + `ExternalAccount`
5. Encrypt + upsert credential blob: `{"ntust_password": ..., "password_verified": false, "token_cache": {"moodle_token": ..., "moodle_private_token": ..., "obtained_at": iso-now}}`, AAD from `build_credential_aad` (review 1.4). Set `credential_status='active'`, `last_auth_success_at=now()`.
6. Upsert `user_devices` on (user_id, client_device_id); clears `deleted_at` if re-registering; sets `last_login_at`/`last_seen_at`.
7. Create `auth_sessions` row (refresh hash, expires now+90d) and issue access JWT.
8. `rate_limiter.reset()` both keys on success.
9. Response: `{access_token, refresh_token, expires_in, user: {id, student_id, display_name}, device_id}`.

`server/main.py`: add `_mount_api_v3(app, settings)` including `auth.router`; called from `create_app` when `settings.api_v3_base_path` non-empty. Build `app.state.moodle_verifier` and `app.state.login_limiter` in `create_app` (not lifespan, so tests can override before requests).

- [ ] Tests (client fixture + StaticMoodleVerifier override): success creates user/account/credential/device/session and returns tokens; second login same student reuses user; Moodle-fail → 401 and no user row; 6th rapid attempt → 429; soft-deleted user is reactivated; credential blob decrypts and contains `password_verified == False`.
- [ ] RED → implement → GREEN (run full test suite)
- [ ] Commit: `feat(Auth): add v3 login endpoint with credential storage and rate limiting`

### Task 9: Refresh rotation + reuse detection

**Files:** Modify `server/auth/service.py`, `server/routes/auth.py`; Test `server/tests/test_auth_refresh.py`

`POST /v3/auth/refresh` body `{refresh_token}`. Service logic:
1. Hash token, look up session. Not found → 401 `invalid_refresh_token`.
2. `expires_at <= now` → mark `revoked_reason='expired'` (if not revoked) → 401.
3. `revoked_at IS NOT NULL`:
   - reason=='rotated' AND `revoked_at > now - grace` (review 1.5): benign retry. Revoke the successor chain (walk `replaced_by_session_id` forward, set `revoked_reason='rotated'` … successor gets `revoked_reason='reuse_detected'`? No — successor is the *lost* token; revoke it with reason `'rotated'` is wrong too. Use `revoked_reason='reuse_detected'`? The chosen semantic: revoke successors with reason `'rotated'` is misleading; use `'reuse_detected'` only for malicious. Decision: revoke successors with reason `'logout'`? None fit perfectly — add no new enum value; use `'rotated'` for the superseded successor (it was rotated *away*). Then create a fresh session chained from the reused one and return new tokens.
   - otherwise (outside grace, or other reason): set `reuse_detected_at=now()` on the session; revoke the entire family — walk `replaced_by_session_id` chain forward from this session and revoke every non-revoked session with reason `'reuse_detected'`; also revoke all active sessions with the same `device_id` when not NULL → 401 `refresh_reuse_detected`.
4. Valid: rotate — revoke old (`reason='rotated'`, `replaced_by_session_id=new.id`), create new session (same user/device, fresh 90-day expiry), return new access+refresh.

- [ ] Tests: normal rotation works and old token then 401s *after* grace expiry (use a settings override `auth_refresh_reuse_grace_seconds=0` variant for the malicious case); retry-within-grace returns fresh tokens and invalidates the lost successor; reuse outside grace revokes the whole chain (third token also 401s); expired session 401s.
- [ ] RED → implement → GREEN
- [ ] Commit: `feat(Auth): add refresh rotation with grace window and family reuse revocation`

### Task 10: Logout + Bearer dependency

**Files:** Create `server/auth/dependencies.py`; Modify `server/routes/auth.py`; Test in `server/tests/test_auth_refresh.py` (logout cases)

```python
@dataclass(frozen=True)
class CurrentAuth:
    user_id: uuid.UUID
    session_id: int
    device_id: uuid.UUID | None

async def require_user(request, authorization: str = Header(...)) -> CurrentAuth
    # parses "Bearer <jwt>", decode_access_token, 401 on any failure
CurrentAuthDep = Annotated[CurrentAuth, Depends(require_user)]
```

`POST /v3/auth/logout` (Bearer required): revoke the JWT's session (`revoked_reason='logout'`); idempotent 204.

- [ ] Tests: logout revokes (refresh with that session's token → 401); missing/garbage Bearer → 401.
- [ ] RED → implement → GREEN
- [ ] Commit: `feat(Auth): add bearer dependency and logout endpoint`

### Task 11: v3 device endpoints

**Files:** Create `server/routes/user_devices.py`; Modify `server/main.py` (include router); Test `server/tests/test_user_devices_v3.py`

All require Bearer auth; device ownership enforced by `user_id` from claims.

- `POST /v3/devices/register` body `{client_device_id, platform, device_name?, app_version?, os_version?, push_token?: {provider, token_kind, token_value, bundle_id?, topic?, environment?, scope_key?}}`: upsert `user_devices` (resurrect soft-deleted); if `push_token` present: sha256 → upsert `device_push_tokens` on the active-unique key (`ON CONFLICT` on partial unique index via manual select-then-insert: select active row by (provider, token_kind, token_hash, scope_key); if exists and belongs to another device, move it to this device (token moved between devices); else insert). Returns device + token ids.
- `GET /v3/devices`: list caller's non-deleted devices (id, client_device_id, platform, device_name, app_version, os_version, last_seen_at, created_at).
- `DELETE /v3/devices/{device_id}`: 404 if not caller's; soft-delete device, revoke its `auth_sessions` (`revoked_reason='admin_revoked'`), set its push tokens `status='invalidated'` (review fix: device deletion revokes access). 204.

- [ ] Tests: register creates device + token; re-register same client_device_id updates not duplicates; register token already active on another device moves it; list excludes soft-deleted; delete → sessions revoked (that device's refresh token 401s) and tokens invalidated; cross-user delete → 404.
- [ ] RED → implement → GREEN (full suite)
- [ ] Commit: `feat(Devices): add v3 device registration, listing, and revoking deletion`

### Task 12: Final verification

- [ ] `uv run pytest -q` full suite green.
- [ ] `uv run ruff check server/ && uv run ruff format --check server/` (if ruff configured; otherwise skip).
- [ ] `uv run alembic upgrade head` against dev DB (verify migration applies cleanly), `alembic downgrade -1` then `upgrade head` again.
- [ ] Commit any fixes: `fix(Auth): ...`

---

## Self-Review Notes

- Spec coverage: all 8 Phase-1 tables (Task 2/3), all 6 Phase-1 endpoints (`login`/`refresh`/`logout` Tasks 8–10, `devices register/list/delete` Task 11). Dual-write to `device_registrations` intentionally remains a frontend responsibility (documented above).
- Review-fix coverage: 1.2 (dedupe index widened, Task 2), 1.3 (Task 6+8), 1.4 (Task 8 blob), 1.5 (Task 9), 1.7 (Task 8 step 4). 1.1/1.8/1.9 belong to Phase 2+; 1.6 (deletion purge) needs the account-deletion endpoint — deferred to Phase 2 plan with a TODO recorded here.
- Type consistency: `CredentialCipher`/`EncryptedBlob` names used in Tasks 4 and 8; `AccessClaims`/`CurrentAuth` in Tasks 5/10; `MoodleVerifyResult` in Tasks 7/8.
