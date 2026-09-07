#!/usr/bin/env bash
# Stop the TigerDuck stack. Volumes are preserved.
#
# Reads TIGERDUCK_ENV from .env: when "development", also loads
# docker-compose.dev.yml so the down command targets the same compose
# topology start.sh brought up.
#
# Usage:
#   ./stop.sh
set -euo pipefail
cd "$(dirname "$0")"
source ./_compose-files.sh

docker compose "${COMPOSE_FILE_ARGS[@]}" down
