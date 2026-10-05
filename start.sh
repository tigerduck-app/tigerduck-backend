#!/usr/bin/env bash
# Bring the whole TigerDuck stack up (build images if needed, start every
# service: postgres, backend, portal, monitoring), then print a status block.
# Idempotent: safe to re-run after editing code, docker-compose.yml, or .env.
#
# Reads TIGERDUCK_ENV from .env: when "development", also loads
# docker-compose.dev.yml (publishes backend 40000, portal 40010, Grafana
# 40020 and Prometheus 40021 to the host, drops proxy-net).
# See the README's Deployment section.
#
# Usage:
#   ./start.sh
set -euo pipefail
cd "$(dirname "$0")"
source ./_compose-files.sh

ensure_monitor_db_password
docker compose "${COMPOSE_FILE_ARGS[@]}" up -d --build
echo
echo "[start] stack is up. logs: ./logs.sh"
print_stack_status
