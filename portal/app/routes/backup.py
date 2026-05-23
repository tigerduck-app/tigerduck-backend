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

import io
import json
import subprocess
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

router = APIRouter(prefix="/backup")

BUNDLE_VERSION = 1


def _pg_env(database_url: str) -> tuple[list[str], dict[str, str]]:
    """Parse a `postgresql+asyncpg://user:pw@host:port/db` URL into the
    args + env that pg_dump / pg_restore want."""
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
    env = {"PGPASSWORD": parsed.password or ""}
    return args, env


@router.get("", response_class=HTMLResponse)
async def page(request: Request) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(request, "backup.html", {})


@router.post("/export")
async def export_bundle(request: Request) -> StreamingResponse:
    """Build the .tar.gz in a temp dir, then stream it to the client.

    Building on disk first (instead of streaming pg_dump → tar live)
    keeps the memory footprint flat and lets pg_dump fail loudly before
    we've sent any bytes.
    """
    settings = request.app.state.settings
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    bundle_name = f"tigerduck-export-{ts}"

    with tempfile.TemporaryDirectory(prefix="tigerduck-export-") as tmp:
        tmpdir = Path(tmp)

        pg_args, pg_env = _pg_env(settings.database_url)
        pg_dump_path = tmpdir / "postgres.dump"
        with pg_dump_path.open("wb") as fh:
            proc = subprocess.run(
                ["pg_dump", *pg_args, "--format=custom", "--no-owner"],
                env=pg_env,
                stdout=fh,
                stderr=subprocess.PIPE,
                check=False,
            )
        if proc.returncode != 0:
            raise HTTPException(
                500,
                f"pg_dump failed: {proc.stderr.decode(errors='replace')[:500]}",
            )

        manifest = {
            "version": BUNDLE_VERSION,
            "exported_at": ts,
            "backend_env": settings.env,
        }
        (tmpdir / "manifest.json").write_text(json.dumps(manifest, indent=2))

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(tmpdir, arcname=bundle_name)
        buf.seek(0)

    return StreamingResponse(
        buf,
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{bundle_name}.tar.gz"'},
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
    payload = await file.read()
    if not payload:
        raise HTTPException(400, "empty upload")

    is_tar = file.filename and file.filename.lower().endswith((".tar.gz", ".tgz"))

    with tempfile.TemporaryDirectory(prefix="tigerduck-import-") as tmp:
        tmpdir = Path(tmp)

        if is_tar:
            try:
                with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
                    tar.extractall(tmpdir, filter="data")
            except (tarfile.TarError, EOFError) as exc:
                raise HTTPException(400, f"could not read .tar.gz: {exc}") from None

            roots = [p for p in tmpdir.iterdir() if p.is_dir()]
            if not roots:
                raise HTTPException(400, "tar contains no directory")
            content = roots[0]

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
            pg_dump_path = tmpdir / "postgres.dump"
            pg_dump_path.write_bytes(payload)

        if not pg_dump_path.exists():
            raise HTTPException(400, "no postgres.dump in upload")

        pg_args, pg_env = _pg_env(settings.database_url)
        proc = subprocess.run(
            ["pg_restore", *pg_args, "--clean", "--if-exists", "--no-owner"],
            input=pg_dump_path.read_bytes(),
            env=pg_env,
            capture_output=True,
            check=False,
        )
        # pg_restore exits non-zero for any warning by default; downgrade
        # warnings to "soft fail" by inspecting stderr.
        if proc.returncode != 0 and b"ERROR" in proc.stderr:
            raise HTTPException(
                500,
                f"pg_restore failed: {proc.stderr.decode(errors='replace')[:500]}",
            )

    return RedirectResponse(
        "/backup?imported=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )
