"""Application settings loaded from environment / .env."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

SERVER_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SERVER_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        env_prefix="TIGERDUCK_",
        extra="ignore",
        case_sensitive=False,
    )

    # --- App ---
    env: Literal["development", "production"] = "development"
    log_level: str = "INFO"
    # Shared-secret that clients must send as X-Push-Token on write endpoints.
    # Empty string disables auth (dev/test convenience). Production must set
    # a non-empty value via TIGERDUCK_API_SHARED_SECRET.
    api_shared_secret: str = ""

    # --- v3 user accounts / auth ---
    api_v3_base_path: str = "/v3"
    # HS256 signing key for access JWTs. Empty means /v3 auth is unconfigured
    # (login returns 503) — mirrors the api_shared_secret dev convention.
    auth_jwt_secret: str = ""
    # Separate HMAC-SHA-256 key for refresh-token hashing (key separation:
    # a leaked JWT secret must not let an attacker forge refresh hashes).
    auth_refresh_hmac_key: str = ""
    auth_access_token_ttl_seconds: int = 900  # 15 min
    auth_refresh_token_ttl_days: int = 90
    # After a refresh rotation, the superseded token stays redeemable for
    # this long so a client whose rotation response was lost in transit can
    # retry without tripping reuse detection (which would log the whole
    # device out). Reuse outside this window IS treated as theft.
    auth_refresh_reuse_grace_seconds: int = 60
    # Login rate limit: N attempts per window, applied independently to the
    # student_id and the client IP. Protects both against credential
    # stuffing and against our single server IP getting blocked by Moodle.
    auth_login_max_attempts: int = 5
    auth_login_window_seconds: int = 900
    # Behind nginx-proxy-manager the socket peer is always the proxy, which
    # would collapse every user into one per-IP rate-limit bucket. Enable
    # this ONLY when a trusted proxy strips/sets X-Forwarded-For; when off,
    # the header is ignored (it is client-spoofable without a proxy).
    auth_trust_forwarded_for: bool = False

    # --- Credential envelope encryption ---
    # key_id -> base64-encoded 32-byte AES-256 key. Multiple entries allow
    # key rotation: new rows encrypt with `credential_active_key_id`, old
    # rows decrypt with whichever key_id they were written under.
    # Env format: TIGERDUCK_CREDENTIAL_KEYS='{"v1":"<base64 32 bytes>"}'
    credential_keys: dict[str, str] = Field(default_factory=dict)
    credential_active_key_id: str = ""

    # --- Moodle token verification (login-time check only) ---
    # Must be the SAME instance the apps harvest the wstoken from
    # (moodle2.ntust.edu.tw) — a wstoken only validates on its issuing host.
    # `moodle.ntust.edu.tw` (no "2") does not resolve → login 401 'unreachable'.
    moodle_base_url: str = "https://moodle2.ntust.edu.tw"
    moodle_verify_timeout_seconds: float = 10.0
    # Default daily maintenance window (Asia/Taipei). During this window,
    # Moodle token-invalid / unreachable errors are treated as transient
    # and the sync job reschedules after the window ends (no notification).
    moodle_maintenance_start: str = "00:00"
    moodle_maintenance_end: str = "05:00"

    # --- Database ---
    # e.g. postgresql+asyncpg://tigerduck:password@localhost:5432/tigerduck
    database_url: str = Field(
        default="postgresql+asyncpg://tigerduck:tigerduck@localhost:5432/tigerduck"
    )
    database_echo: bool = False

    # --- APNs ---
    apns_bundle_id: str = "org.ntust.app.TigerDuck"
    apns_team_id: str = ""
    apns_key_id: str = ""
    apns_key_path: Path = SERVER_DIR / "secrets" / "apns_auth_key.p8"
    # "development" talks to api.sandbox.push.apple.com (debug builds via Xcode)
    # "production" talks to api.push.apple.com (TestFlight / App Store)
    apns_env: Literal["development", "production"] = "development"

    # --- FCM (Android) ---
    # Firebase project id — only required when delivering real pushes.
    # Empty string means "use the recording stub", same convention as APNs.
    fcm_project_id: str = ""
    # Path to the service-account JSON downloaded from the Firebase console.
    # When the file is missing the router falls back to RecordingFcmSender.
    fcm_credentials_path: Path = SERVER_DIR / "secrets" / "fcm_service_account.json"
    # Hard cap on a single FCM send. firebase-admin's sync `messaging.send`
    # has no per-call timeout, so without this a stuck token-mint or
    # unreachable googleapis lookup blocks the bulletin_dispatch tick
    # indefinitely and APScheduler skips every following tick.
    fcm_send_timeout_seconds: float = 15.0

    # --- Scheduler ---
    # how often dispatcher polls DB for due pushes
    scheduler_tick_seconds: int = 30
    # fire_at within [now, now + window_seconds] becomes eligible each tick
    scheduler_window_seconds: int = 60

    # --- Live Activity token retention ---
    # Prune update-token rows that reached a terminal state (ended / failed /
    # cancelled) and have not been touched since `live_activity_token_retention_days`.
    # Active rows are never pruned; a device that stops syncing keeps its
    # pending rows until the device itself is unregistered and the cascade
    # delete fires.
    live_activity_token_retention_days: int = 30
    live_activity_token_retention_interval_hours: int = 24

    # --- Sync change log retention ---
    # Entries older than this are purged; clients further behind than the
    # purge watermark get HTTP 410 and full-sync.
    sync_changelog_retention_days: int = 30
    sync_changelog_retention_interval_hours: int = 24

    # --- Server-side sync jobs (Phase 3) ---
    sync_job_tick_seconds: int = 30
    # Max jobs claimed per tick by ONE worker.
    sync_job_batch_size: int = 5
    # Minimum seconds between individual job executions within a tick.
    # Effectively rate-limits server-side sync to 1 job per interval.
    sync_job_min_interval_seconds: int = 60
    # Maintenance window — no server-side sync during this period.
    # Format: "HH:MM-HH:MM" in UTC, e.g. "02:00-04:00". Empty = no window.
    sync_maintenance_window: str = ""
    # Cap on `status='running'` rows ACROSS all workers — counted before
    # claiming so multiple instances can't collectively hammer the school
    # APIs from our single egress IP (security review suggestion).
    sync_job_global_concurrency: int = 5
    sync_job_stale_lock_minutes: int = 10
    # Retriable-failure backoff: base * 2^(attempts-1), capped.
    sync_job_backoff_base_seconds: int = 300
    sync_job_backoff_cap_seconds: int = 3600
    # Pull-to-refresh per-user-per-job-type cooldown.
    sync_job_manual_cooldown_seconds: int = 60
    # Moodle webservice fetch timeout (sync worker, not login verify).
    moodle_fetch_timeout_seconds: float = 20.0

    # --- User push pipeline (Phase 4) ---
    push_pipeline_tick_seconds: int = 30
    push_pipeline_batch_size: int = 10
    push_job_stale_lock_minutes: int = 5
    # Delay before a job with still-pending deliveries gets another round.
    push_retry_round_delay_seconds: int = 60

    # --- Assignment reminders (Phase 4a) ---
    assignment_reminder_scan_interval_seconds: int = 300
    # Server-side default when a user has no `notification` settings
    # document. Deliberately lighter than the client's six-offset default.
    assignment_reminder_default_offsets_hours: list[float] = Field(
        default_factory=lambda: [24.0, 2.0]
    )
    # Assignments due further out than this are picked up by a later scan;
    # offsets larger than the window are unsupported (the reminder would
    # be born in the past).
    assignment_reminder_window_hours: int = 168

    # --- Course reminders (Phase 4b) ---
    course_reminder_scan_interval_seconds: int = 600
    course_reminder_window_hours: int = 48
    # Minutes before class start; used when the user has no `notification`
    # settings document (or no `courses.reminder_offsets_minutes`).
    course_reminder_default_offsets_minutes: list[float] = Field(
        default_factory=lambda: [10.0]
    )
    course_reminder_timezone: str = "Asia/Taipei"
    # NTUST period number → local class start time (HH:MM). schedule_json
    # periods are normalized via str() before lookup; unknown periods are
    # ignored so a future timetable change degrades to "no reminder", not
    # a crash.
    course_period_start_times: dict[str, str] = Field(
        default_factory=lambda: {
            "1": "08:10",
            "2": "09:10",
            "3": "10:20",
            "4": "11:20",
            "5": "12:20",
            "6": "13:20",
            "7": "14:20",
            "8": "15:30",
            "9": "16:30",
            "10": "17:30",
            "A": "18:25",
            "B": "19:20",
            "C": "20:15",
            "D": "21:10",
        }
    )

    # --- Bulletins ---
    bulletin_list_url: str = (
        "https://bulletin.ntust.edu.tw/p/403-1045-1391-1.php"
    )
    bulletin_scrape_interval_seconds: int = 600   # 10 min
    bulletin_process_interval_seconds: int = 60
    bulletin_dispatch_interval_seconds: int = 60
    # Phase 4c: user-level (logged-in) bulletin fan-out via push_jobs.
    bulletin_user_dispatch_interval_seconds: int = 60
    bulletin_user_dispatch_batch_size: int = 10
    # Rows whose last_seen_at is older than N scrape cycles get is_deleted=true.
    bulletin_stale_cycles: int = 3
    # Max processing retries before giving up on a bulletin.
    bulletin_max_process_attempts: int = 3
    # Delete is_deleted=true rows this old to keep the table bounded. Rows
    # still visible on the bulletin board keep refreshing last_seen_at and
    # stay forever; only the ones that fell off the list and aged out go.
    bulletin_retention_days: int = 365
    # Retention job runs at this cadence. Once a day is plenty.
    bulletin_retention_interval_hours: int = 24
    # Optional PEM file bundling NTUST's root + intermediates. When set and
    # readable, the bulletin HTTP client uses it as the trust anchor so
    # OpenSSL can complete the chain (the NTUST servers themselves ship
    # incomplete chains). When unset, the client falls back to the MVP
    # behavior of `verify=False` — still functional, but skips hostname /
    # chain validation. Obtain the bundle via `openssl s_client -showcerts`
    # against each NTUST subdomain the pipeline reaches.
    bulletin_ca_bundle: Path | None = None

    # --- LLM (OpenAI-compatible endpoint: llama.cpp, Gemini, vLLM, ...) ---
    llm_base_url: str = "http://localhost:8080/v1"
    llm_api_key: str = "sk-local"
    llm_model: str = "gemma-4-e4b-it"
    # 120s is generous because multi-slot llama.cpp fans one backend
    # GPU across concurrent requests, so effective per-request latency
    # scales with the backfill `--concurrency`. 30s was too tight for
    # Gemma-4 E4B on Apple Silicon at 3× concurrency.
    llm_timeout_seconds: float = 120.0
    llm_max_retries: int = 2
    llm_temperature: float = 0.2
    # Dev-only escape hatch: when true, server/main.py's _wait_for_llm
    # returns immediately instead of polling the LLM /models endpoint
    # for up to 60s. Useful when iterating locally without llama-server
    # running (e.g. UI / push work that doesn't touch bulletins). The
    # bulletin classification job still tries the LLM on its own ticks
    # and self-heals once llama-server is back up.
    skip_llm_probe: bool = False

    @property
    def apns_topic_live_activity(self) -> str:
        return f"{self.apns_bundle_id}.push-type.liveactivity"


def get_settings() -> Settings:
    return Settings()
