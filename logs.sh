#!/usr/bin/env bash
# Tail backend logs. Pass a service name to tail something else (e.g. postgres).
#
# Reads TIGERDUCK_ENV from .env: when "development", also loads
# docker-compose.dev.yml. Compose's project discovery would find the
# running service either way, but matching start.sh's compose-files
# invocation keeps every script consistent.
#
# Usage:
#   ./logs.sh              # backend
#   ./logs.sh postgres     # postgres
set -euo pipefail
cd "$(dirname "$0")"
source ./_compose-files.sh

svc="${1:-backend}"
exec docker compose "${COMPOSE_FILE_ARGS[@]}" logs -f --tail=200 "$svc"
