#!/usr/bin/env bash
# Encrypted off-site backups of the compose postgres to Cloudflare R2, and a
# restore drill that proves the newest one restores. Runs on the host, from
# launchd (deploy/launchd/ai.tigerduck.backup.plist); the runbook is the
# "Upgrading to 3.4.0" section of MIGRATE.md. Written for macOS /bin/bash 3.2.
#
#   tigerduck-backup.sh backup         dump, encrypt, upload, prune past 30 days
#   tigerduck-backup.sh drill          restore the newest backup into a throwaway container
#   tigerduck-backup.sh nightly        backup, then the drill on Sundays (launchd runs this)
#   tigerduck-backup.sh list           list the backups in the bucket, oldest first
#   tigerduck-backup.sh fetch [NAME]   download and decrypt NAME (default: newest) into ./
#
# Settings come from the checkout's .env (TIGERDUCK_BACKUP_*). Each success is
# recorded in ops.backup_status, which sql-exporter turns into
# tigerduck_backup_last_success_timestamp_seconds for the alerts.
set -euo pipefail
umask 077

# Appended, not prepended, so a PATH set by the caller wins.
PATH="${PATH}:/opt/homebrew/bin:/usr/local/bin"

REPO_DIR="${TIGERDUCK_REPO_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
ENV_FILE="${REPO_DIR}/.env"
DB_CONTAINER="tigerduck-db"
PREFIX="postgres"
OBJECT_GLOB="tigerduck-*.dump.gpg"
RETENTION="30d"
DRILL_IMAGE="postgres:17-alpine"
DRILL_TABLES="users user_devices device_push_tokens user_courses user_assignments push_jobs bulletins"
SETTINGS="TIGERDUCK_BACKUP_S3_ENDPOINT TIGERDUCK_BACKUP_S3_BUCKET TIGERDUCK_BACKUP_S3_ACCESS_KEY_ID TIGERDUCK_BACKUP_S3_SECRET_ACCESS_KEY TIGERDUCK_BACKUP_PASSPHRASE"

WORK=""
DRILL_CONTAINER=""

usage() {
    sed -n 's/^#   \(tigerduck-backup.sh \)/usage: \1/p' "$0" >&2
    exit 2
}

die() {
    echo "tigerduck-backup: error: $*" >&2
    exit 1
}

log() {
    echo "tigerduck-backup: $(date -u +%Y-%m-%dT%H:%M:%SZ) $*"
}

cleanup() {
    if [ -n "${DRILL_CONTAINER}" ]; then
        docker rm -f "${DRILL_CONTAINER}" >/dev/null 2>&1 || true
    fi
    if [ -n "${WORK}" ]; then
        rm -rf "${WORK}"
    fi
}

# Parsed rather than sourced, so nothing else in .env runs or is exported.
env_value() {
    local line value=""
    while IFS= read -r line || [ -n "${line}" ]; do
        case "${line}" in
            "$1="*) value="${line#*=}" ;;
        esac
    done <"${ENV_FILE}"
    value="${value%$'\r'}"
    case "${value}" in
        \"*\") value="${value#\"}" && value="${value%\"}" ;;
        \'*\') value="${value#\'}" && value="${value%\'}" ;;
    esac
    printf '%s' "${value}"
}

load_settings() {
    [ -r "${ENV_FILE}" ] || die "cannot read ${ENV_FILE}"
    local name missing=""
    for name in ${SETTINGS}; do
        unset "${name}"
        printf -v "${name}" '%s' "$(env_value "${name}")"
        [ -n "${!name}" ] || missing="${missing} ${name}"
    done
    [ -z "${missing}" ] || die "not set in ${ENV_FILE}:${missing}"
    REMOTE="tigerduck_r2:${TIGERDUCK_BACKUP_S3_BUCKET}/${PREFIX}"
}

require_tools() {
    local tool missing=""
    for tool in "$@"; do
        command -v "${tool}" >/dev/null 2>&1 || missing="${missing} ${tool}"
    done
    [ -z "${missing}" ] || die "not on PATH:${missing}"
}

# The remote comes from the environment only: no config file, no secret on argv.
r2() {
    RCLONE_CONFIG_TIGERDUCK_R2_TYPE=s3 \
    RCLONE_CONFIG_TIGERDUCK_R2_PROVIDER=Cloudflare \
    RCLONE_CONFIG_TIGERDUCK_R2_REGION=auto \
    RCLONE_CONFIG_TIGERDUCK_R2_NO_CHECK_BUCKET=true \
    RCLONE_CONFIG_TIGERDUCK_R2_ENDPOINT="${TIGERDUCK_BACKUP_S3_ENDPOINT}" \
    RCLONE_CONFIG_TIGERDUCK_R2_ACCESS_KEY_ID="${TIGERDUCK_BACKUP_S3_ACCESS_KEY_ID}" \
    RCLONE_CONFIG_TIGERDUCK_R2_SECRET_ACCESS_KEY="${TIGERDUCK_BACKUP_S3_SECRET_ACCESS_KEY}" \
        rclone --config /dev/null --quiet "$@"
}

# The passphrase goes in on stdin from a builtin, never on a command line.
gpg_with_passphrase() {
    printf '%s\n' "${TIGERDUCK_BACKUP_PASSPHRASE}" |
        gpg --batch --quiet --yes --pinentry-mode loopback --no-symkey-cache \
            --passphrase-fd 0 "$@"
}

list_backups() {
    r2 lsf --files-only --max-depth 1 --include "${OBJECT_GLOB}" "${REMOTE}/" | sort
}

newest_backup() {
    local newest
    newest="$(list_backups | tail -n 1)" || die "listing ${REMOTE}/ failed"
    [ -n "${newest}" ] || die "no backups under ${REMOTE}/"
    printf '%s' "${newest}"
}

download_and_decrypt() {
    local name="$1" out="$2"
    r2 copyto "${REMOTE}/${name}" "${WORK}/${name}" || die "download of ${name} failed"
    gpg_with_passphrase --output "${out}" --decrypt "${WORK}/${name}" ||
        die "decrypting ${name} failed"
    rm -f "${WORK}/${name}"
}

record_success() {
    docker exec -i "${DB_CONTAINER}" psql -U tigerduck -d tigerduck -q \
        -v ON_ERROR_STOP=1 -v kind="$1" >/dev/null <<'SQL' ||
INSERT INTO ops.backup_status (kind, last_success_at) VALUES (:'kind', now())
ON CONFLICT (kind) DO UPDATE SET last_success_at = EXCLUDED.last_success_at;
SQL
        die "could not record the $1 in ops.backup_status (run ./start.sh so monitor-role creates it)"
}

cmd_backup() {
    local name dump
    name="tigerduck-$(date -u +%Y%m%dT%H%M%SZ).dump.gpg"
    dump="${WORK}/tigerduck.dump"
    docker exec "${DB_CONTAINER}" \
        pg_dump -U tigerduck -d tigerduck --format=custom --no-owner >"${dump}" ||
        die "pg_dump in ${DB_CONTAINER} failed"
    [ -s "${dump}" ] || die "pg_dump wrote nothing"
    # pg_dump's custom format is compressed already.
    gpg_with_passphrase --symmetric --cipher-algo AES256 --compress-algo none \
        --output "${WORK}/${name}" "${dump}" || die "encryption failed"
    rm -f "${dump}"
    r2 copyto "${WORK}/${name}" "${REMOTE}/${name}" || die "upload of ${name} failed"
    log "uploaded ${name} ($(wc -c <"${WORK}/${name}" | tr -d ' ') bytes)"
    rm -f "${WORK}/${name}"
    record_success backup
    r2 delete --min-age "${RETENTION}" --max-depth 1 --include "${OBJECT_GLOB}" "${REMOTE}/" ||
        die "pruning backups older than ${RETENTION} failed"
}

wait_for_drill_postgres() {
    for _ in $(seq 1 60); do
        # Over TCP: the image's init-time server listens on the socket only.
        if docker exec "${DRILL_CONTAINER}" pg_isready -q -h 127.0.0.1 -U tigerduck; then
            return 0
        fi
        sleep 1
    done
    die "the drill's postgres did not start within 60 s"
}

drill_sql() {
    docker exec "${DRILL_CONTAINER}" psql -U tigerduck -d tigerduck -X -A -t \
        -v ON_ERROR_STOP=1 -c "$1"
}

cmd_drill() {
    local name revision table count counts=""
    name="$(newest_backup)"
    download_and_decrypt "${name}" "${WORK}/drill.dump"
    DRILL_CONTAINER="tigerduck-backup-drill-$$"
    # No network: nothing can reach this copy, and it can reach nothing.
    docker run -d --rm --name "${DRILL_CONTAINER}" --network none \
        -e POSTGRES_USER=tigerduck -e POSTGRES_DB=tigerduck \
        -e POSTGRES_HOST_AUTH_METHOD=trust "${DRILL_IMAGE}" >/dev/null ||
        die "could not start the drill container"
    wait_for_drill_postgres
    # Roles are not in the dump; their grants would fail here.
    docker exec -i "${DRILL_CONTAINER}" pg_restore -U tigerduck -d tigerduck \
        --no-owner --no-privileges --exit-on-error <"${WORK}/drill.dump" ||
        die "pg_restore of ${name} failed"
    revision="$(drill_sql 'SELECT version_num FROM alembic_version')" ||
        die "the restored ${name} has no alembic_version"
    [ -n "${revision}" ] || die "the restored ${name} has an empty alembic_version"
    for table in ${DRILL_TABLES}; do
        count="$(drill_sql "SELECT count(*) FROM ${table}")" ||
            die "the restored ${name} has no ${table} table"
        counts="${counts} ${table}=${count}"
    done
    docker rm -f "${DRILL_CONTAINER}" >/dev/null 2>&1 || true
    DRILL_CONTAINER=""
    log "drill restored ${name}: alembic=${revision}${counts}"
    record_success drill
}

cmd_fetch() {
    local name="${1:-}"
    if [ -z "${name}" ]; then
        name="$(newest_backup)"
    fi
    case "${name}" in
        */* | ..*) die "not a backup name: ${name}" ;;
    esac
    [ ! -e "${name%.gpg}" ] || die "${name%.gpg} exists already"
    download_and_decrypt "${name}" "${name%.gpg}"
    log "wrote $(pwd)/${name%.gpg}"
}

main() {
    [ "$#" -ge 1 ] || usage
    local command="$1"
    shift
    case "${command}" in
        backup | drill | nightly | list) [ "$#" -eq 0 ] || usage ;;
        fetch) [ "$#" -le 1 ] || usage ;;
        *) usage ;;
    esac

    load_settings
    case "${command}" in
        list) require_tools rclone ;;
        fetch) require_tools rclone gpg ;;
        *) require_tools rclone gpg docker ;;
    esac

    trap cleanup EXIT
    trap 'exit 130' INT TERM HUP
    WORK="$(mktemp -d "${TMPDIR:-/tmp}/tigerduck-backup.XXXXXX")"

    case "${command}" in
        backup) cmd_backup ;;
        drill) cmd_drill ;;
        nightly)
            cmd_backup
            if [ "$(date +%u)" = 7 ]; then
                cmd_drill
            fi
            ;;
        list) list_backups ;;
        fetch) cmd_fetch "$@" ;;
    esac
}

main "$@"
