"""deploy/backup/tigerduck-backup.sh, run against stub rclone, gpg and docker.

No test reaches R2, a database or Docker: the stubs on PATH log how they
were called. What matters is that the script refuses to run half
configured and that no secret ever lands on a command line.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

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

STUBS = {
    # Logs argv, and whether the remote came through the environment.
    "rclone": """#!/bin/bash
echo "argv: $*" >> "$STUB_LOG"
echo "env secret: ${RCLONE_CONFIG_TIGERDUCK_R2_SECRET_ACCESS_KEY:-}" >> "$STUB_LOG.env"
""",
    # Logs argv, the passphrase it read on fd 3 and the data it read on
    # stdin; writes its --output.
    "gpg": """#!/bin/bash
echo "argv: $*" >> "$STUB_LOG"
read -r passphrase <&3
echo "fd 3 passphrase: $passphrase" >> "$STUB_LOG.env"
echo "stdin: $(cat)" >> "$STUB_LOG.env"
while [ "$#" -gt 0 ]; do
    if [ "$1" = --output ]; then echo encrypted > "$2"; fi
    shift
done
""",
    # pg_dump writes a dump; psql reads its SQL from stdin.
    "docker": """#!/bin/bash
echo "argv: $*" >> "$STUB_LOG"
case "$*" in
    *pg_dump*) echo PGDMP ;;
    *psql*) cat >> "$STUB_LOG.sql" ;;
esac
""",
}


def _run(tmp_path: Path, *args: str, settings: dict[str, str] = SETTINGS):
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
    }
    result = subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env,
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


def test_missing_settings_are_named_and_nothing_runs(tmp_path):
    partial = {
        "TIGERDUCK_BACKUP_S3_ENDPOINT": "https://r2.invalid",
        "TIGERDUCK_BACKUP_S3_SECRET_ACCESS_KEY": SECRET,
        "TIGERDUCK_BACKUP_PASSPHRASE": "",
    }

    result, calls, _ = _run(tmp_path, "backup", settings=partial)

    assert result.returncode == 1
    for name in (
        "TIGERDUCK_BACKUP_S3_BUCKET",
        "TIGERDUCK_BACKUP_S3_ACCESS_KEY_ID",
        "TIGERDUCK_BACKUP_PASSPHRASE",
    ):
        assert name in result.stderr
    assert "TIGERDUCK_BACKUP_S3_ENDPOINT" not in result.stderr
    assert SECRET not in result.stdout + result.stderr
    assert calls == ""


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


def test_backup_removes_its_temporary_files_and_lock(tmp_path):
    result, _, _ = _run(tmp_path, "backup")

    assert result.returncode == 0, result.stderr
    assert not list(tmp_path.glob("tigerduck-backup*"))


def test_a_run_while_another_holds_the_lock_exits_cleanly(tmp_path):
    lock = tmp_path / "tigerduck-backup-lock"
    lock.symlink_to(str(os.getpid()))

    result, calls, _ = _run(tmp_path, "nightly")

    assert result.returncode == 0, result.stderr
    assert "still going" in result.stdout
    assert calls == ""
    assert lock.is_symlink()


def test_a_lock_left_by_a_killed_run_is_taken_over_and_released(tmp_path):
    gone = subprocess.Popen(["true"])
    gone.wait()
    dead_pid = gone.pid
    lock = tmp_path / "tigerduck-backup-lock"
    lock.symlink_to(str(dead_pid))

    result, calls, _ = _run(tmp_path, "backup")

    assert result.returncode == 0, result.stderr
    assert "copyto" in calls
    assert not lock.is_symlink()


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
