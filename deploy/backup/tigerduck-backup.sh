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
TMP_BASE="${TMPDIR:-/tmp}"
LOCK="${TMP_BASE%/}/tigerduck-backup.lock"
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
RUNNING=""

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
    local status=$?
    if [ -n "${DRILL_CONTAINER}" ]; then
        docker rm -f "${DRILL_CONTAINER}" >/dev/null 2>&1 || true
    fi
    if [ -n "${WORK}" ]; then
        rm -rf "${WORK}"
    fi
    if [ "${status}" -ne 0 ] && [ -n "${RUNNING}" ]; then
        record last_failure_at "${RUNNING}" >/dev/null 2>&1 || true
    fi
}

# A kernel lock on fd 9, which this shell holds until it exits however it
# exits, so a killed run leaves nothing stale. macOS has no flock(1); its perl does it.
acquire_lock() {
    exec 9>>"${LOCK}"
    if ! perl -MFcntl=:flock -e 'open(my $fh, ">&=", 9) or die "fd 9: $!\n"; flock($fh, LOCK_EX | LOCK_NB) or exit 1'; then
        log "another run is still going; exiting"
        exit 0
    fi
}

# Docker Desktop may still be starting after a reboot or a wake at 04:30.
wait_for_docker() {
    for _ in $(seq 1 60); do
        if docker info >/dev/null 2>&1; then
            return 0
        fi
        sleep 10
    done
    die "docker did not answer for 10 minutes"
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

# The passphrase comes on fd 3 from a builtin, never on a command line, which
# leaves stdin and stdout for the data. fd 9 is closed so a gpg-agent it
# starts cannot keep holding the lock.
gpg_with_passphrase() {
    gpg --batch --quiet --yes --pinentry-mode loopback --no-symkey-cache \
        --passphrase-fd 3 "$@" 3< <(printf '%s\n' "${TIGERDUCK_BACKUP_PASSPHRASE}") 9>&-
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

download() {
    r2 copyto "${REMOTE}/$1" "${WORK}/$1" || die "download of $1 failed"
}

# record last_success_at|last_failure_at backup|drill
record() {
    docker exec -i "${DB_CONTAINER}" psql -U tigerduck -d tigerduck -q \
        -v ON_ERROR_STOP=1 -v kind="$2" >/dev/null <<SQL
INSERT INTO ops.backup_status (kind, $1) VALUES (:'kind', now())
ON CONFLICT (kind) DO UPDATE SET $1 = EXCLUDED.$1;
SQL
}

record_success() {
    record last_success_at "$1" ||
        die "could not record the $1 in ops.backup_status (run ./start.sh so monitor-role creates it)"
    RUNNING=""
}

cmd_backup() {
    local name
    RUNNING=backup
    name="tigerduck-$(date -u +%Y%m%dT%H%M%SZ).dump.gpg"
    # Streamed, so no unencrypted dump touches the disk; custom format is
    # compressed already.
    docker exec "${DB_CONTAINER}" pg_dump -U tigerduck -d tigerduck --format=custom --no-owner |
        gpg_with_passphrase --symmetric --cipher-algo AES256 --compress-algo none \
            --output "${WORK}/${name}" ||
        die "pg_dump in ${DB_CONTAINER} or its encryption failed"
    r2 copyto "${WORK}/${name}" "${REMOTE}/${name}" || die "upload of ${name} failed"
    log "uploaded ${name} ($(wc -c <"${WORK}/${name}" | tr -d ' ') bytes)"
    rm -f "${WORK}/${name}"
    r2 delete --min-age "${RETENTION}" --max-depth 1 --include "${OBJECT_GLOB}" "${REMOTE}/" ||
        die "pruning backups older than ${RETENTION} failed"
    record_success backup
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
    local name orphan revision table count counts=""
    RUNNING=drill
    name="$(newest_backup)"
    download "${name}"
    # Left by a killed run; the lock means no other drill is going.
    for orphan in $(docker ps -aq --filter name=tigerduck-backup-drill); do
        docker rm -f "${orphan}" >/dev/null
    done
    DRILL_CONTAINER="tigerduck-backup-drill-$$"
    # No network: nothing can reach this copy, and it can reach nothing.
    docker run -d --rm --name "${DRILL_CONTAINER}" --network none \
        -e POSTGRES_USER=tigerduck -e POSTGRES_DB=tigerduck \
        -e POSTGRES_HOST_AUTH_METHOD=trust "${DRILL_IMAGE}" >/dev/null ||
        die "could not start the drill container"
    wait_for_drill_postgres
    # Roles are not in the dump; their grants would fail here.
    gpg_with_passphrase --decrypt "${WORK}/${name}" |
        docker exec -i "${DRILL_CONTAINER}" pg_restore -U tigerduck -d tigerduck \
            --no-owner --no-privileges --exit-on-error ||
        die "decrypting or restoring ${name} failed"
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
    download "${name}"
    gpg_with_passphrase --output "${name%.gpg}" --decrypt "${WORK}/${name}" ||
        die "decrypting ${name} failed"
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

    # Set before the checks, so a run that cannot start is recorded as failed.
    case "${command}" in
        backup | nightly) RUNNING=backup ;;
        drill) RUNNING=drill ;;
    esac
    trap cleanup EXIT
    trap 'exit 130' INT TERM HUP

    load_settings
    case "${command}" in
        list) require_tools rclone ;;
        fetch) require_tools rclone gpg ;;
        *) require_tools rclone gpg docker ;;
    esac

    case "${command}" in
        backup | drill | nightly)
            acquire_lock
            # Work dirs a killed run left behind.
            find "${TMP_BASE}" -maxdepth 1 -type d -name 'tigerduck-backup.*' -mtime +0 \
                -exec rm -rf {} + 2>/dev/null || true
            wait_for_docker
            ;;
    esac
    WORK="$(mktemp -d "${TMP_BASE%/}/tigerduck-backup.XXXXXX")"

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
