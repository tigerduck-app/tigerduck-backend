#!/bin/sh
# Create or refresh the read-only `tigerduck_monitor` role that
# postgres-exporter and Grafana's SQL panels log in as.
#
# Run by the one-shot `monitor-role` compose service on every `up`, so an
# existing volume, a fresh one after clean-db.sh, and a changed
# TIGERDUCK_MONITOR_DB_PASSWORD all end up with a correct role. Roles live
# outside pg_dump's per-database output, so a portal import doesn't touch it.
#
# pg_monitor        — pg_stat_activity with every session's query, plus the
#                     stats views the exporter reads.
# pg_read_all_data  — SELECT on every table, present and future, for SQL
#                     panels. Grafana runs whatever SQL a panel holds, so the
#                     login must not be able to write; read-only transactions
#                     are the second guard.
# CONNECTION LIMIT  — a runaway dashboard can't eat into the 100 connections
#                     the backend and portal share.
set -eu

: "${MONITOR_DB_PASSWORD:?MONITOR_DB_PASSWORD is empty — set TIGERDUCK_MONITOR_DB_PASSWORD or POSTGRES_PASSWORD in .env}"

psql -v ON_ERROR_STOP=1 -v pw="$MONITOR_DB_PASSWORD" <<'SQL'
-- Re-granting an existing membership is a NOTICE on every run after the first.
SET client_min_messages = warning;
SELECT 'CREATE ROLE tigerduck_monitor'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'tigerduck_monitor')
\gexec
ALTER ROLE tigerduck_monitor
    WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE CONNECTION LIMIT 10
    PASSWORD :'pw';
ALTER ROLE tigerduck_monitor SET default_transaction_read_only = on;
GRANT pg_monitor, pg_read_all_data TO tigerduck_monitor;
SQL

echo "[monitor-role] tigerduck_monitor is ready"
