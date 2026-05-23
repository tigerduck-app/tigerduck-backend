# Sourced by start.sh / stop.sh / logs.sh / clean-db.sh.
#
# Reads TIGERDUCK_ENV from .env. When the value is "development", appends
# the docker-compose.dev.yml override (publishes port 40000, drops
# proxy-net). Otherwise leaves the prod-shaped base file alone.
#
# Why grep-based instead of `set -a; source .env`: the .env file is a
# docker-compose dotenv file, not a shell file, and may contain values
# that aren't valid shell syntax (e.g. URLs with $-signs, unquoted
# special chars). Sourcing risks shell expansion side effects; grep
# just reads the literal value.
#
# Exports COMPOSE_FILE_ARGS as an array. Callers use:
#   docker compose "${COMPOSE_FILE_ARGS[@]}" up -d --build
#
# Strict match on "development" — any other value (production, staging,
# typo) falls through to prod behaviour. This way an unrecognised value
# can't accidentally publish the backend port on a production server.

# Read an arbitrary TIGERDUCK_* (or any) key from .env. The
# `grep || true` swallows grep's non-zero exit when the key is absent,
# so callers under `set -e` + `pipefail` get an empty string instead of
# a script-killing failure.
_dotenv_value() {
    local key="$1"
    [[ -f .env ]] || { echo ""; return 0; }
    { grep -E "^${key}=" .env || true; } | tail -n1 | cut -d= -f2- | tr -d '"' | tr -d "'"
}

_tigerduck_env_value() {
    _dotenv_value TIGERDUCK_ENV
}

COMPOSE_FILE_ARGS=(-f docker-compose.yml)
if [[ "$(_tigerduck_env_value)" == "development" ]]; then
    COMPOSE_FILE_ARGS+=(-f docker-compose.dev.yml)
fi

# Print a one-block status summary tying together what the user just
# started. Called by start.sh / clean-db.sh AFTER `docker compose up`.
# Each line shows the human-meaningful state, not raw env-var names.
print_stack_status() {
    local env_val skip_llm apns_env log_level llm_url backend_url
    env_val="$(_tigerduck_env_value)"
    skip_llm="$(_dotenv_value TIGERDUCK_SKIP_LLM_PROBE)"
    apns_env="$(_dotenv_value TIGERDUCK_APNS_ENV)"
    log_level="$(_dotenv_value TIGERDUCK_LOG_LEVEL)"
    llm_url="$(_dotenv_value TIGERDUCK_LLM_BASE_URL)"

    local portal_url
    case "$env_val" in
        development)
            backend_url="http://localhost:40000/v2 (published to host)"
            portal_url="http://localhost:40010 (published to host)"
            ;;
        production)
            backend_url="http://tigerduck-internal:40000/v2 (proxy-net only)"
            portal_url="http://tigerduck-portal:40010 (proxy-net only)"
            ;;
        *)
            backend_url="http://tigerduck-internal:40000/v2 (proxy-net only — TIGERDUCK_ENV='$env_val' treated as prod)"
            portal_url="http://tigerduck-portal:40010 (proxy-net only — TIGERDUCK_ENV='$env_val' treated as prod)"
            ;;
    esac

    case "$skip_llm" in
        true|TRUE|1|yes) skip_llm="yes (LLM probe will return immediately)" ;;
        *)               skip_llm="no (waits up to 60s for LLM at startup)" ;;
    esac

    local bootstrap_admin
    bootstrap_admin="$(_dotenv_value TIGERDUCK_PORTAL_BOOTSTRAP_ADMIN)"

    echo "  ─────────────────────────────────────────────────────────────"
    echo "  mode             : ${env_val:-(unset, treated as prod)}"
    echo "  log level        : ${log_level:-INFO (default)}"
    echo "  backend          : ${backend_url}"
    echo "  portal           : ${portal_url}"
    echo "  apns env         : ${apns_env:-(unset)}"
    echo "  llm probe        : ${skip_llm}"
    echo "  llm url          : ${llm_url:-(unset)}"
    echo "  portal bootstrap : ${bootstrap_admin:-(unset — local dev allowed)}"
    echo "  ─────────────────────────────────────────────────────────────"
}
