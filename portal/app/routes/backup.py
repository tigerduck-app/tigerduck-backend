"""Backup export / import.

Export streams a freshly-built .tar.gz to the browser. Import accepts
either an export bundle or a bare pg_dump file (for migrating from a
pre-portal install). Importing does NOT restart the backend container —
the portal keeps a read-only docker socket on purpose; the user is told
to run `./stop.sh && ./start.sh` once the restore finishes.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

router = APIRouter(prefix="/api/backup")

BUNDLE_VERSION = 1


def _pg_env(database_url: str) -> tuple[list[str], dict[str, str]]:
    """Translate `postgresql+asyncpg://user:pw@host:port/db` into the
    args + env that pg_dump / pg_restore want.

    Merges PGPASSWORD into the host environment so the child keeps
    PATH/LANG/etc. — passing `{"PGPASSWORD": ...}` alone would strip
    everything else libpq / locale setup might depend on.
    """
    cleaned = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    parsed = urlparse(cleaned)
    if not parsed.hostname:
        raise HTTPException(500, "TIGERDUCK_DATABASE_URL is malformed")
    args = [
        "-h", parsed.hostname,
        "-p", str(parsed.port or 5432),
        "-U", parsed.username or "tigerduck",
        "-d", (parsed.path or "/tigerduck").lstrip("/"),
    ]
    env = {**os.environ, "PGPASSWORD": parsed.password or ""}
    return args, env


@router.post("/export")
async def export_bundle(request: Request) -> FileResponse:
    """Build the archive on disk in a temp dir, stream it, then let a
    BackgroundTask reap the dir after the response flushes — keeps peak
    memory bounded by an OS pipe buffer rather than the dump size.
    """
    settings = request.app.state.settings
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    bundle_name = f"tigerduck-export-{ts}"

    # mkdtemp (not TemporaryDirectory) so the directory outlives this
    # function — FileResponse streams from disk after we return.
    workdir = Path(tempfile.mkdtemp(prefix="tigerduck-export-"))
    cleanup = BackgroundTask(shutil.rmtree, str(workdir), ignore_errors=True)
    try:
        staging = workdir / bundle_name
        staging.mkdir()

        pg_args, pg_env = _pg_env(settings.database_url)
        pg_dump_path = staging / "postgres.dump"
        with pg_dump_path.open("wb") as fh:
            proc = await asyncio.create_subprocess_exec(
                "pg_dump", *pg_args, "--format=custom", "--no-owner",
                env=pg_env,
                stdout=fh,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise HTTPException(
                500,
                f"pg_dump failed: {stderr.decode(errors='replace')[:500]}",
            )

        manifest = {
            "version": BUNDLE_VERSION,
            "exported_at": ts,
            "backend_env": settings.env,
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2))

        tar_path = workdir / f"{bundle_name}.tar.gz"
        with tarfile.open(tar_path, "w:gz") as tar:
            tar.add(staging, arcname=bundle_name)
    except Exception:
        shutil.rmtree(workdir, ignore_errors=True)
        raise

    return FileResponse(
        tar_path,
        media_type="application/gzip",
        filename=f"{bundle_name}.tar.gz",
        background=cleanup,
    )


@router.post("/import")
async def import_bundle(request: Request, file: UploadFile) -> JSONResponse:
    """Accept either a portal-export .tar.gz or a bare pg_dump file.

    Returns JSON instead of a redirect — the SPA shows the success
    banner itself. Backend container is left untouched; the response
    asks the user to restart it from the host.
    """
    settings = request.app.state.settings
    is_tar = bool(
        file.filename and file.filename.lower().endswith((".tar.gz", ".tgz"))
    )

    with tempfile.TemporaryDirectory(prefix="tigerduck-import-") as tmp:
        tmpdir = Path(tmp)

        # Stream chunks to disk so a multi-GB dump doesn't have to fit
        # in the portal container's heap.
        upload_path = tmpdir / ("upload.tar.gz" if is_tar else "postgres.dump")
        with upload_path.open("wb") as fh:
            shutil.copyfileobj(file.file, fh)

        if upload_path.stat().st_size == 0:
            raise HTTPException(400, "empty upload")

        if is_tar:
            try:
                with tarfile.open(upload_path, mode="r:gz") as tar:
                    tar.extractall(tmpdir, filter="data")
            except (tarfile.TarError, EOFError) as exc:
                raise HTTPException(400, f"could not read .tar.gz: {exc}") from None

            # Sort + filter so root choice is deterministic (iterdir
            # order is FS-dependent) and macOS Archive Utility's
            # __MACOSX/ ghost dir is ignored.
            roots = sorted(
                p for p in tmpdir.iterdir()
                if p.is_dir() and p.name != "__MACOSX"
            )
            content = next(
                (r for r in roots if (r / "postgres.dump").exists()),
                roots[0] if roots else None,
            )
            if content is None:
                raise HTTPException(400, "tar contains no directory")

            manifest_path = content / "manifest.json"
            if manifest_path.exists():
                manifest = json.loads(manifest_path.read_text())
                if manifest.get("version", 0) > BUNDLE_VERSION:
                    raise HTTPException(
                        400,
                        f"bundle version {manifest['version']} is newer than this portal "
                        f"(supports up to {BUNDLE_VERSION}). Upgrade the portal first.",
                    )

            pg_dump_path = content / "postgres.dump"
        else:
            pg_dump_path = upload_path

        if not pg_dump_path.exists():
            raise HTTPException(400, "no postgres.dump in upload")

        pg_args, pg_env = _pg_env(settings.database_url)
        # Positional file path (not stdin) keeps the dump out of
        # Python memory and gives pg_restore clearer error output
        # when the format is wrong.
        proc = await asyncio.create_subprocess_exec(
            "pg_restore", *pg_args, "--clean", "--if-exists", "--no-owner",
            str(pg_dump_path),
            env=pg_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            # Earlier soft-fail logic grepped stderr for "ERROR" and
            # missed bad-format uploads (pg_restore writes lowercase
            # "error:") — treat any non-zero exit as failure.
            raise HTTPException(
                500,
                f"pg_restore failed: {stderr.decode(errors='replace')[:500]}",
            )

    return JSONResponse({"ok": True, "imported_at": datetime.now(UTC).isoformat()})
