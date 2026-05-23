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

# Detect host LAN IPs so they can be surfaced in the start.sh status
# block AND threaded into the portal container (it has no view of the
# host's network interfaces from inside its own netns).
#
# Strategy: enumerate the names that are virtually always real LAN
# interfaces on macOS (en0..en5) and Linux (eth*, wlan*) and keep only
# RFC1918 addresses. On Linux with non-standard names (enp3s0, ens33,
# enx*) we fall back to a `ip addr show` scan.
_detect_lan_ips() {
    local ips=""
    local iface ip
    for iface in en0 en1 en2 en3 en4 en5 eth0 eth1 eth2 wlan0 wlan1; do
        ip="$(ifconfig "$iface" 2>/dev/null | awk '/inet /{print $2; exit}')" || true
        if [[ -n "$ip" ]]; then
            case "$ip" in
                10.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[01].*)
                    ips+="${ip}"$'\n'
                    ;;
            esac
        fi
    done
    if [[ -z "$ips" && "$(uname)" == "Linux" ]]; then
        ips="$(ip -4 addr show 2>/dev/null \
            | awk '/inet /{print $2}' \
            | cut -d/ -f1 \
            | grep -E '^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)' \
            | grep -v '^127\.' || true)"
        ips="${ips}"$'\n'
    fi
    printf '%s' "$ips" | awk 'NF' | sort -u
}

# Comma-joined form for the env var that docker-compose interpolates
# into the portal container. Exported so `${TIGERDUCK_HOST_LAN_IPS:-}`
# in docker-compose.yml picks it up.
TIGERDUCK_HOST_LAN_IPS="$(_detect_lan_ips | paste -sd, -)"
export TIGERDUCK_HOST_LAN_IPS

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

    local bootstrap_admin backend_version
    bootstrap_admin="$(_dotenv_value TIGERDUCK_PORTAL_BOOTSTRAP_ADMIN)"
    # Read straight off disk so we don't depend on the backend being up
    # (and don't pay an HTTP round-trip here). Authoritative source is
    # server/__init__.py::__version__.
    backend_version="$(grep -oE '__version__[[:space:]]*=[[:space:]]*"[^"]+"' server/__init__.py 2>/dev/null \
        | sed -E 's/.*"([^"]+)".*/\1/' \
        | head -n1)"

    echo "  ─────────────────────────────────────────────────────────────"
    echo "  mode             : ${env_val:-(unset, treated as prod)}"
    echo "  log level        : ${log_level:-INFO (default)}"
    echo "  backend version  : ${backend_version:-(unknown — server/__init__.py not parsable)}"
    echo "  backend          : ${backend_url}"
    echo "  portal           : ${portal_url}"
    echo "  apns env         : ${apns_env:-(unset)}"
    echo "  llm probe        : ${skip_llm}"
    echo "  llm url          : ${llm_url:-(unset)}"
    echo "  portal bootstrap : ${bootstrap_admin:-(unset — local dev allowed)}"
    if [[ -n "${TIGERDUCK_HOST_LAN_IPS:-}" ]]; then
        # Print one URL per IP per port so the user can copy/paste a
        # phone-reachable address directly from the terminal.
        echo "  ─────────────────────────────────────────────────────────────"
        echo "  LAN access (reachable from other devices on the same network):"
        local ip
        IFS=',' read -ra _lan_ips <<< "$TIGERDUCK_HOST_LAN_IPS"
        for ip in "${_lan_ips[@]}"; do
            [[ -z "$ip" ]] && continue
            echo "    backend  http://${ip}:40000"
            echo "    portal   http://${ip}:40010"
        done
    else
        echo "  lan ips          : (none detected — no RFC1918 address on en*/eth*/wlan*)"
    fi
    echo "  ─────────────────────────────────────────────────────────────"
}
