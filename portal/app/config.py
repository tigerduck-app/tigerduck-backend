"""Portal config — pydantic-settings reads env at startup.

All keys use the `TIGERDUCK_PORTAL_` prefix EXCEPT the database/llm/etc.
values that are shared with the backend (those keep the backend's
`TIGERDUCK_` prefix so a single .env feeds both services).
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Portal-side settings.

    Note: we don't use a prefix because we need to read BOTH portal-
    specific keys (TIGERDUCK_PORTAL_*) and backend keys (TIGERDUCK_*).
    Field-name → env-var alias is set explicitly via the `alias` arg.
    """

    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    # --- Portal own state ---
    portal_db_path: str = "/data/portal.db"
    # Email seeded into the admins table on every startup so a lockout
    # is impossible. Empty (default) means "no bootstrap" — useful for
    # smoke tests but in real use this should be set.
    portal_bootstrap_admin: str = ""

    # --- Shared with backend (for the status page) ---
    env: str = "production"
    apns_env: str = ""
    skip_llm_probe: bool = False
    llm_base_url: str = ""
    log_level: str = "INFO"
    database_url: str = ""
    apns_key_path: str = ""
    fcm_credentials_path: str = ""

    # Public URLs surfaced on the status page so an operator can hop
    # straight from "is it up?" to "open the thing." Defaults match
    # whatever print_stack_status would print: dev → localhost ports,
    # prod → proxy-net internal hostnames (override with the env vars
    # to point at your public DNS name).
    backend_public_url: str = ""
    portal_public_url: str = ""

    # Host LAN IPs threaded in by start.sh / _compose-files.sh — the
    # portal can't introspect the host network from inside the container.
    # Comma-separated input is parsed in from_env.
    host_lan_ips: list[str] = []

    # --- Cloudflare Access (when fronted by cloudflared) ---
    # Header name CF Access injects. Override only for tests.
    cf_access_email_header: str = "Cf-Access-Authenticated-User-Email"

    @classmethod
    def from_env(cls) -> "Settings":
        """Map TIGERDUCK_* / TIGERDUCK_PORTAL_* env vars onto fields.

        Done manually (not via pydantic's prefix machinery) because we
        cross two prefixes. Keeps the field names clean.
        """
        import os

        env = os.environ
        env_mode = env.get("TIGERDUCK_ENV", "production")
        # Match _compose-files.sh's URL choices so the status page and
        # the start.sh stdout never disagree.
        if env_mode == "development":
            default_backend = "http://localhost:40000"
            default_portal = "http://localhost:40010"
        else:
            default_backend = "http://tigerduck-internal:40000"
            default_portal = "http://tigerduck-portal:40010"
        return cls(
            portal_db_path=env.get(
                "TIGERDUCK_PORTAL_DB_PATH", "/data/portal.db"
            ),
            portal_bootstrap_admin=env.get(
                "TIGERDUCK_PORTAL_BOOTSTRAP_ADMIN", ""
            ),
            env=env_mode,
            apns_env=env.get("TIGERDUCK_APNS_ENV", ""),
            skip_llm_probe=(
                env.get("TIGERDUCK_SKIP_LLM_PROBE", "false").lower()
                in {"true", "1", "yes"}
            ),
            llm_base_url=env.get("TIGERDUCK_LLM_BASE_URL", ""),
            log_level=env.get("TIGERDUCK_LOG_LEVEL", "INFO"),
            database_url=env.get("TIGERDUCK_DATABASE_URL", ""),
            apns_key_path=env.get("TIGERDUCK_APNS_KEY_PATH", ""),
            fcm_credentials_path=env.get(
                "TIGERDUCK_FCM_CREDENTIALS_PATH", ""
            ),
            backend_public_url=env.get(
                "TIGERDUCK_BACKEND_PUBLIC_URL", default_backend
            ),
            portal_public_url=env.get(
                "TIGERDUCK_PORTAL_PUBLIC_URL", default_portal
            ),
            host_lan_ips=[
                ip.strip()
                for ip in env.get("TIGERDUCK_HOST_LAN_IPS", "").split(",")
                if ip.strip()
            ],
            cf_access_email_header=env.get(
                "TIGERDUCK_PORTAL_CF_ACCESS_HEADER",
                "Cf-Access-Authenticated-User-Email",
            ),
        )
