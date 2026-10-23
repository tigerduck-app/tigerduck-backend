"""deploy/backup/tigerduck-backup.sh, run against stub rclone, gpg and docker.

No test reaches R2, a database or Docker: the stubs on PATH log how they
were called, and STUB_FAIL makes one step fail. What matters is that the
script refuses to run half configured, that no secret ever lands on a
command line, and that every outcome of a backup or drill is recorded.
"""

from __future__ import annotations

import fcntl
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "backup" / "tigerduck-backup.sh"

SECRET = "s3cr3t-access-value"
PASSPHRASE = "correct horse battery staple"
SETTINGS = {
    "TIGERDUCK_BACKUP_S3_ENDPOINT": "https://r2.invalid",
    "TIGERDUCK_BACKUP_S3_BUCKET": '"tigerduck-backups"',
    "TIGERDUCK_BACKUP_S3_ACCESS_KEY_ID": "'key-id'",
    "TIGERDUCK_BACKUP_S3_SECRET_ACCESS_KEY": SECRET,
    "TIGERDUCK_BACKUP_PASSPHRASE": PASSPHRASE,
}

NEWEST = "tigerduck-20260102T000000Z.dump.gpg"

STUBS = {
    # Logs argv and whether the remote came through the environment; lists
    # two backups, newest last once sorted; a download writes the file.
    "rclone": """#!/bin/bash
echo "argv: $*" >> "$STUB_LOG"
echo "env secret: ${RCLONE_CONFIG_TIGERDUCK_R2_SECRET_ACCESS_KEY:-}" >> "$STUB_LOG.env"
case "$4" in
    lsf) printf '%s\\n' tigerduck-20260102T000000Z.dump.gpg tigerduck-20260101T000000Z.dump.gpg ;;
    copyto)
        case "$5" in tigerduck_r2:*) echo encrypted > "$6" ;; esac
        case "$6" in tigerduck_r2:*) if [ "${STUB_FAIL:-}" = upload ]; then exit 1; fi ;; esac ;;
    delete) if [ "${STUB_FAIL:-}" = prune ]; then exit 1; fi ;;
esac
""",
    # Logs argv, the passphrase from fd 3 and what --symmetric read on
    # stdin; writes --output, or the decrypted dump to stdout.
    "gpg": """#!/bin/bash
echo "argv: $*" >> "$STUB_LOG"
read -r passphrase <&3
echo "fd 3 passphrase: $passphrase" >> "$STUB_LOG.env"
case " $* " in
    *" --symmetric "*) echo "stdin: $(cat)" >> "$STUB_LOG.env" ;;
esac
case " $* " in
    *" --output "*)
        while [ "$#" -gt 0 ]; do
            if [ "$1" = --output ]; then echo encrypted > "$2"; fi
            shift
        done ;;
    *" --decrypt "*)
        if [ "${STUB_FAIL:-}" = decrypt ]; then exit 2; fi
        echo PGDMP-decrypted ;;
esac
""",
    # The live database and the drill container: pg_dump writes a dump,
    # pg_restore and the recording psql read stdin, drill queries answer.
    "docker": """#!/bin/bash
echo "argv: $*" >> "$STUB_LOG"
case "$*" in
    "ps -aq"*) echo orphan123 ;;
    *pg_dump*) echo PGDMP ;;
    *pg_restore*)
        echo "restore stdin: $(cat)" >> "$STUB_LOG.env"
        if [ "${STUB_FAIL:-}" = pg_restore ]; then exit 1; fi ;;
    *"SELECT version_num"*) echo 516f9bad27f0 ;;
    *"FROM users"*)
        if [ "${STUB_FAIL:-}" = table ]; then exit 1; fi
        echo 3 ;;
    *"SELECT count"*) echo 3 ;;
    *psql*) cat >> "$STUB_LOG.sql" ;;
esac
""",
    "date": """#!/bin/bash
if [ "$1" = +%u ]; then echo "${STUB_WEEKDAY:-3}"; else exec /bin/date "$@"; fi
""",
}


def _run(
    tmp_path: Path,
    *args: str,
    settings: dict[str, str] = SETTINGS,
    **stub_env: str,
):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    lines = ["POSTGRES_PASSWORD=unrelated"]
    lines += [f"{key}={value}" for key, value in settings.items()]
    (repo / ".env").write_text("\n".join(lines) + "\n")
    stubs = tmp_path / "bin"
    stubs.mkdir(exist_ok=True)
    for name, body in STUBS.items():
        path = stubs / name
        path.write_text(body)
        path.chmod(0o755)
    log = tmp_path / "calls.log"
    env = {
        "PATH": f"{stubs}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "TIGERDUCK_REPO_DIR": str(repo),
        "STUB_LOG": str(log),
        **stub_env,
    }
    result = subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    calls = log.read_text() if log.exists() else ""
    return result, calls, log


def test_the_script_parses():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_an_unknown_command_prints_usage(tmp_path):
    result, calls, _ = _run(tmp_path, "restore-live")

    assert result.returncode == 2
    assert "usage: tigerduck-backup.sh backup" in result.stderr
    assert calls == ""


def test_extra_arguments_print_usage(tmp_path):
    result, calls, _ = _run(tmp_path, "backup", "now")

    assert result.returncode == 2
    assert calls == ""


def test_missing_settings_are_named_and_recorded_and_nothing_else_runs(tmp_path):
    partial = {
        "TIGERDUCK_BACKUP_S3_ENDPOINT": "https://r2.invalid",
        "TIGERDUCK_BACKUP_S3_SECRET_ACCESS_KEY": SECRET,
        "TIGERDUCK_BACKUP_PASSPHRASE": "",
    }

    result, calls, log = _run(tmp_path, "backup", settings=partial)

    assert result.returncode == 1
    for name in (
        "TIGERDUCK_BACKUP_S3_BUCKET",
        "TIGERDUCK_BACKUP_S3_ACCESS_KEY_ID",
        "TIGERDUCK_BACKUP_PASSPHRASE",
    ):
        assert name in result.stderr
    assert "TIGERDUCK_BACKUP_S3_ENDPOINT" not in result.stderr
    assert SECRET not in result.stdout + result.stderr
    assert "rclone" not in calls and "pg_dump" not in calls
    assert "last_failure_at" in Path(f"{log}.sql").read_text()


def test_backup_keeps_secrets_off_every_command_line(tmp_path):
    result, calls, log = _run(tmp_path, "backup")

    assert result.returncode == 0, result.stderr
    assert SECRET not in calls
    assert PASSPHRASE not in calls
    assert SECRET not in result.stdout + result.stderr
    assert PASSPHRASE not in result.stdout + result.stderr
    secrets_seen = Path(f"{log}.env").read_text()
    assert f"env secret: {SECRET}" in secrets_seen
    assert f"fd 3 passphrase: {PASSPHRASE}" in secrets_seen
    # The dump reaches gpg on a pipe; no unencrypted file is written.
    assert "stdin: PGDMP" in secrets_seen
    assert ".dump\n" not in calls

    # Quotes around .env values are not part of the bucket name.
    upload = re.search(
        r"argv: .*copyto \S+ tigerduck_r2:tigerduck-backups/postgres/"
        r"(tigerduck-\d{8}T\d{6}Z\.dump\.gpg)\n",
        calls,
    )
    assert upload, calls
    assert "delete --min-age 30d --max-depth 1 --include tigerduck-*.dump.gpg" in calls
    assert "kind=backup" in calls
    assert "INSERT INTO ops.backup_status" in Path(f"{log}.sql").read_text()


def test_backup_removes_its_temporary_files(tmp_path):
    result, _, _ = _run(tmp_path, "backup")

    assert result.returncode == 0, result.stderr
    assert not [p for p in tmp_path.glob("tigerduck-backup.*") if p.is_dir()]


def test_a_run_while_another_holds_the_lock_exits_cleanly(tmp_path):
    with open(tmp_path / "tigerduck-backup.lock", "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)

        result, calls, _ = _run(tmp_path, "nightly")

    assert result.returncode == 0, result.stderr
    assert "still going" in result.stdout
    assert calls == ""


def test_a_lock_file_nobody_holds_does_not_block(tmp_path):
    # What a killed run leaves: the file, but no lock on it.
    (tmp_path / "tigerduck-backup.lock").touch()

    result, calls, _ = _run(tmp_path, "backup")

    assert result.returncode == 0, result.stderr
    assert "copyto" in calls


@pytest.mark.parametrize(
    ("step", "message"), [("upload", "upload of"), ("prune", "pruning backups")]
)
def test_a_failed_backup_is_recorded_as_a_failure(tmp_path, step, message):
    result, calls, log = _run(tmp_path, "backup", STUB_FAIL=step)

    assert result.returncode == 1
    assert message in result.stderr
    assert "kind=backup" in calls
    recorded = Path(f"{log}.sql").read_text()
    assert "last_failure_at" in recorded
    assert "last_success_at" not in recorded


def test_drill_restores_the_newest_backup_and_records_success(tmp_path):
    result, calls, log = _run(tmp_path, "drill")

    assert result.returncode == 0, result.stderr
    assert f"drill restored {NEWEST}: alembic=516f9bad27f0 users=3" in result.stdout
    assert f"copyto tigerduck_r2:tigerduck-backups/postgres/{NEWEST}" in calls
    assert "rm -f orphan123" in calls
    drill = re.search(r"run -d --rm --name (tigerduck-backup-drill-\d+) --network none", calls)
    assert drill, calls
    assert f"rm -f {drill.group(1)}" in calls
    # Decrypted on a pipe straight into pg_restore.
    assert "restore stdin: PGDMP-decrypted" in Path(f"{log}.env").read_text()
    assert "--output" not in calls.split("--decrypt")[0].splitlines()[-1]
    assert "kind=drill" in calls
    assert "last_success_at" in Path(f"{log}.sql").read_text()


@pytest.mark.parametrize(
    ("step", "message"),
    [
        ("decrypt", "decrypting or restoring"),
        ("pg_restore", "decrypting or restoring"),
        ("table", "has no users table"),
    ],
)
def test_a_failed_drill_is_recorded_and_cleaned_up(tmp_path, step, message):
    result, calls, log = _run(tmp_path, "drill", STUB_FAIL=step)

    assert result.returncode == 1
    assert message in result.stderr
    drill = re.search(r"--name (tigerduck-backup-drill-\d+)", calls)
    assert drill, calls
    assert f"rm -f {drill.group(1)}" in calls
    recorded = Path(f"{log}.sql").read_text()
    assert "last_failure_at" in recorded
    assert "last_success_at" not in recorded


@pytest.mark.parametrize(("weekday", "drills"), [("7", True), ("3", False)])
def test_nightly_drills_on_sundays_only(tmp_path, weekday, drills):
    result, calls, _ = _run(tmp_path, "nightly", STUB_WEEKDAY=weekday)

    assert result.returncode == 0, result.stderr
    assert "pg_dump" in calls
    assert ("pg_restore" in calls) is drills


def test_work_dirs_older_than_a_day_are_removed(tmp_path):
    stale = tmp_path / "tigerduck-backup.stale1"
    stale.mkdir()
    (stale / "tigerduck.dump").write_text("PGDMP")
    two_days_ago = time.time() - 2 * 86400
    os.utime(stale, (two_days_ago, two_days_ago))
    fresh = tmp_path / "tigerduck-backup.fresh1"
    fresh.mkdir()

    result, _, _ = _run(tmp_path, "backup")

    assert result.returncode == 0, result.stderr
    assert not stale.exists()
    assert fresh.exists()


def test_fetch_refuses_a_path_as_the_backup_name(tmp_path):
    result, calls, _ = _run(tmp_path, "fetch", "../etc/passwd")

    assert result.returncode == 1
    assert "not a backup name" in result.stderr
    assert "copyto" not in calls


def test_the_script_is_executable():
    assert os.access(SCRIPT, os.X_OK)
