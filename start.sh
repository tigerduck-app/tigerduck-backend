#!/usr/bin/env bash
# Bring the TigerDuck stack up (build image if needed, start postgres + backend).
# Idempotent: safe to re-run after editing code, docker-compose.yml, or .env.
#
# Reads TIGERDUCK_ENV from .env: when "development", also loads
# docker-compose.dev.yml (publishes port 40000 to host, drops proxy-net).
# See docs/local-dev-backend.md.
#
# Usage:
#   ./start.sh
set -euo pipefail
cd "$(dirname "$0")"
source ./_compose-files.sh

docker compose "${COMPOSE_FILE_ARGS[@]}" up -d --build
echo
echo "[start] stack is up. logs: ./logs.sh"
print_stack_status
