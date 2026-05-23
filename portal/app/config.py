"""Portal config — pydantic-settings reads env at startup.

Cross-prefix: most keys are backend's `TIGERDUCK_*` (so a single .env
feeds both services), with a few portal-only keys under the
`TIGERDUCK_PORTAL_*` prefix.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    # --- Shared with backend (for the status page) ---
    env: str = "production"
    apns_env: str = ""
    skip_llm_probe: bool = False
    llm_base_url: str = ""
    log_level: str = "INFO"
    database_url: str = ""
    apns_key_path: str = ""
    fcm_credentials_path: str = ""
    # Forwarded as `X-Push-Token` when the portal proxies to the backend's
    # /v2/_debug/* surface (test-push page). Same value the iOS app reads
    # from Secrets.plist. Empty in prod is fine — the test page itself is
    # dev-only and 404s before any backend call would happen.
    api_shared_secret: str = ""

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
            api_shared_secret=env.get("TIGERDUCK_API_SHARED_SECRET", ""),
        )
