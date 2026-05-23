"""Export / import the backend postgres database.

Export bundles:
  - postgres.dump   pg_dump --format=custom of the tigerduck DB
  - manifest.json   { version, exported_at }

Import accepts the same .tar.gz OR a bare postgres.dump (pre-portal
install path). The portal does not restart the backend — after restore
it asks the user to run `./stop.sh && ./start.sh`, which keeps the
docker socket mount read-only.
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
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from starlette.background import BackgroundTask
from starlette.status import HTTP_303_SEE_OTHER

router = APIRouter(prefix="/backup")

BUNDLE_VERSION = 1


def _pg_env(database_url: str) -> tuple[list[str], dict[str, str]]:
    """Parse a `postgresql+asyncpg://user:pw@host:port/db` URL into the
    args + env that pg_dump / pg_restore want.

    Returned env merges the host environment with PGPASSWORD so the
    child process keeps PATH, LANG, and any other vars libpq / locale
    setup might depend on — passing a bare `{"PGPASSWORD": ...}` would
    strip everything else.
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


@router.get("", response_class=HTMLResponse)
async def page(request: Request) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(request, "backup.html", {})


@router.post("/export")
async def export_bundle(request: Request) -> FileResponse:
    """Build the .tar.gz on disk, then stream it to the client.

    pg_dump runs as an asyncio subprocess so it doesn't block the event
    loop (otherwise every other portal request — status page, log poller —
    freezes for the duration of the dump). The finished tar lives in a
    temp dir that's cleaned up by a BackgroundTask after the response
    flushes, so peak memory is bounded by a single OS-level pipe buffer
    rather than the full archive size.
    """
    settings = request.app.state.settings
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    bundle_name = f"tigerduck-export-{ts}"

    # mkdtemp (not TemporaryDirectory) so the directory outlives this
    # function — FileResponse needs the file on disk while it streams.
    # BackgroundTask deletes it once the response is done.
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
        # Run cleanup synchronously since FileResponse won't get a chance to.
        shutil.rmtree(workdir, ignore_errors=True)
        raise

    return FileResponse(
        tar_path,
        media_type="application/gzip",
        filename=f"{bundle_name}.tar.gz",
        background=cleanup,
    )


@router.post("/import")
async def import_bundle(request: Request, file: UploadFile) -> RedirectResponse:
    """Accept either:
      * a .tar.gz produced by the export endpoint above
      * a bare pg_dump custom-format file (for migrating a pre-portal install)

    Backend container stays up; we restore the DB over the network and
    ask the user to restart the backend manually so the portal can stick
    to a read-only docker socket.
    """
    settings = request.app.state.settings
    is_tar = bool(
        file.filename and file.filename.lower().endswith((".tar.gz", ".tgz"))
    )

    with tempfile.TemporaryDirectory(prefix="tigerduck-import-") as tmp:
        tmpdir = Path(tmp)

        # Stream the upload to disk in chunks so a multi-GB dump doesn't
        # have to fit in the portal container's heap.
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

            # Sort + filter so the choice of content root is deterministic
            # (iterdir order is filesystem-dependent) and we ignore macOS
            # Archive Utility's __MACOSX/ ghost dir.
            roots = sorted(
                p for p in tmpdir.iterdir()
                if p.is_dir() and p.name != "__MACOSX"
            )
            # Prefer a root that actually contains postgres.dump so a
            # tar with extra junk directories still resolves correctly.
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
        # Pass the dump path as a positional arg instead of piping via
        # stdin — avoids re-reading the file into Python memory and
        # gives pg_restore a clearer error if the format is wrong.
        proc = await asyncio.create_subprocess_exec(
            "pg_restore", *pg_args, "--clean", "--if-exists", "--no-owner",
            str(pg_dump_path),
            env=pg_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            # Earlier versions tried to soft-fail "warnings only" by
            # grepping stderr for "ERROR" — that missed bad-format
            # uploads (pg_restore writes lowercase "error:") and
            # silently treated broken restores as success. Treat any
            # non-zero exit as failure; operators who want warning
            # tolerance can add --exit-on-error to the cmdline.
            raise HTTPException(
                500,
                f"pg_restore failed: {stderr.decode(errors='replace')[:500]}",
            )

    return RedirectResponse(
        "/backup?imported=1",
        status_code=HTTP_303_SEE_OTHER,
    )
