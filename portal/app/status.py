"""Status-page data gathering.

Each function returns a small dict that the Jinja template renders. All
checks fail SAFE — a missing file, an unreachable DB, or a docker socket
that's been remounted RO are all reflected in the returned dict instead
of raising. The status page should always render even when the world is
on fire.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import asyncpg
import httpx


DOCKER_SOCK = "/var/run/docker.sock"

# The portal sits on the same compose network as the backend container,
# so its DNS name is always reachable regardless of dev/prod port
# publishing. Public URLs in Settings are for what we DISPLAY to the
# user (clickable links); intra-stack health pings use this.
BACKEND_INTERNAL_URL = "http://tigerduck-internal:40000"


async def backend_version(timeout_s: float = 2.0) -> dict[str, Any]:
    """Ask the backend what version it is. Returns
    `{ok, version?, api_base_path?, detail?}`. Fails safe so the status
    page still renders if the backend is restarting."""
    url = f"{BACKEND_INTERNAL_URL}/version"
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            r = await client.get(url)
    except httpx.HTTPError as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    if r.status_code >= 400:
        return {"ok": False, "detail": f"HTTP {r.status_code}"}
    try:
        body = r.json()
    except ValueError:
        return {"ok": False, "detail": "non-JSON response"}
    return {
        "ok": True,
        "version": body.get("version"),
        "api_base_path": body.get("api_base_path"),
    }


async def llm_health(llm_base_url: str, timeout_s: float = 3.0) -> dict[str, Any]:
    """Try a single GET against the configured LLM /models endpoint."""
    if not llm_base_url:
        return {"reachable": False, "detail": "TIGERDUCK_LLM_BASE_URL not set"}
    url = llm_base_url.rstrip("/") + "/models"
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            r = await client.get(url)
            return {
                "reachable": r.status_code < 400,
                "status_code": r.status_code,
                "latency_ms": round((time.monotonic() - start) * 1000),
                "url": url,
            }
    except httpx.HTTPError as exc:
        return {
            "reachable": False,
            "detail": f"{type(exc).__name__}: {exc}",
            "url": url,
        }


async def postgres_health(database_url: str, timeout_s: float = 3.0) -> dict[str, Any]:
    """Connect, count rows in a few core tables. Returns shape:
    {"reachable": bool, "alembic_head": str|None, "rows": {...}}.

    Uses asyncpg's own timeout= for connect (asyncio.wait_for around it
    can leave an in-progress TCP/TLS handshake running after cancellation
    and leak a socket). Per-query timeouts cap each fetchval at the
    remaining wall-time budget so a status-page hit can't hang
    indefinitely if postgres is holding a lock (e.g. mid-pg_restore).
    """
    if not database_url:
        return {"reachable": False, "detail": "TIGERDUCK_DATABASE_URL not set"}

    # asyncpg wants the postgres:// scheme, not postgresql+asyncpg://.
    asyncpg_url = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    conn: asyncpg.Connection | None = None
    try:
        try:
            conn = await asyncpg.connect(asyncpg_url, timeout=timeout_s)
        except (asyncio.TimeoutError, OSError, asyncpg.PostgresError) as exc:
            return {"reachable": False, "detail": f"{type(exc).__name__}: {exc}"}

        try:
            alembic_head = await asyncio.wait_for(
                conn.fetchval("SELECT version_num FROM alembic_version LIMIT 1"),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            alembic_head = None
        rows: dict[str, int | str] = {}
        for table in ("device_registrations", "bulletins", "scheduled_pushes"):
            try:
                rows[table] = await asyncio.wait_for(
                    conn.fetchval(f"SELECT count(*) FROM {table}"),
                    timeout=timeout_s,
                )
            except asyncio.TimeoutError:
                rows[table] = "error: timed out"
            except asyncpg.PostgresError as exc:
                rows[table] = f"error: {exc}"
    finally:
        if conn is not None:
            await conn.close()

    return {
        "reachable": True,
        "alembic_head": alembic_head,
        "rows": rows,
    }


async def docker_containers(timeout_s: float = 3.0) -> list[dict[str, Any]]:
    """Query the docker engine API over the mounted unix socket.

    Why HTTP-over-UDS instead of shelling out to `docker inspect`: the
    portal container doesn't ship the docker CLI (and shouldn't — saves
    ~150 MB and the `docker.io` Debian package's CLI placement is
    distribution-dependent). The engine's REST API is the contract.

    Containers that don't exist are omitted (not an error — a fresh
    install hasn't started the backend yet). Hard failures (socket
    missing, engine unreachable) surface as a single synthetic row so
    the page still tells the operator something is wrong.
    """
    names = ["tigerduck-db", "tigerduck-internal", "tigerduck-portal"]
    if not Path(DOCKER_SOCK).exists():
        return [
            {
                "name": "(docker socket)",
                "state": "unreachable",
                "detail": f"{DOCKER_SOCK} not mounted into the portal container",
            }
        ]

    rows: list[dict[str, Any]] = []
    transport = httpx.AsyncHTTPTransport(uds=DOCKER_SOCK)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://docker",
            timeout=timeout_s,
        ) as client:
            for name in names:
                try:
                    r = await client.get(f"/containers/{name}/json")
                except httpx.HTTPError as exc:
                    rows.append(
                        {
                            "name": name,
                            "state": "unreachable",
                            "detail": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    continue
                if r.status_code == 404:
                    continue
                if r.status_code >= 400:
                    rows.append(
                        {
                            "name": name,
                            "state": "error",
                            "detail": f"engine HTTP {r.status_code}",
                        }
                    )
                    continue
                payload = r.json()
                state = payload.get("State", {})
                rows.append(
                    {
                        "name": name,
                        "state": state.get("Status", "?"),
                        "health": (state.get("Health") or {}).get("Status"),
                        "started_at": state.get("StartedAt"),
                        "restart_count": payload.get("RestartCount", 0),
                        "image": payload.get("Config", {}).get("Image", ""),
                    }
                )
    finally:
        await transport.aclose()
    return rows


def _resolve_secret_path(path_str: str) -> Path | None:
    """Map a configured secret path onto the portal container's view.

    Backend resolves relative paths against /app; the portal sees the same
    directory mounted at /backend-secrets. Returns None when path_str is
    empty (caller decides whether 'not configured' is OK).
    """
    if not path_str:
        return None
    p = Path(path_str)
    if not p.is_absolute():
        p = Path("/backend-secrets") / p.name
    return p


def fcm_config(env_project_id: str, path_str: str) -> dict[str, Any]:
    """Cross-check the env-declared FCM project id against the JSON file.

    States:
      * `ok`        — env value present, JSON readable, project ids match
      * `mismatch`  — both present but the JSON's project_id differs
      * `missing`   — exactly one of (env value, JSON file) present
      * `disabled`  — neither configured (intentional — recording stub)

    `project_id` in the response is the env value when present, otherwise
    the JSON's project_id when only that side exists, otherwise None.
    """
    env_pid = env_project_id.strip() or None
    p = _resolve_secret_path(path_str)
    json_pid: str | None = None
    json_error: str | None = None
    if p is not None:
        try:
            if p.exists():
                with p.open("r", encoding="utf-8") as f:
                    body = json.load(f)
                pid = body.get("project_id")
                if pid:
                    json_pid = str(pid)
                else:
                    json_error = "project_id missing in JSON"
            else:
                json_error = "file not found"
        except (OSError, ValueError) as exc:
            json_error = f"{type(exc).__name__}: {exc}"

    if env_pid is None and json_pid is None and json_error is None:
        return {"state": "disabled", "detail": "not configured"}
    if env_pid and json_pid and env_pid != json_pid:
        return {
            "state": "mismatch",
            "project_id": env_pid,
            "json_project_id": json_pid,
            "detail": f"env={env_pid} but JSON={json_pid}",
        }
    if env_pid is None:
        return {
            "state": "missing",
            "project_id": json_pid,
            "detail": "TIGERDUCK_FCM_PROJECT_ID unset",
        }
    if json_pid is None:
        return {
            "state": "missing",
            "project_id": env_pid,
            "detail": json_error or "JSON missing",
        }
    return {"state": "ok", "project_id": env_pid}


def apns_config(
    apns_env: str,
    team_id: str,
    key_id: str,
    key_path_str: str,
) -> dict[str, Any]:
    """Cross-check the env-declared APNs identity against the .p8 file.

    There's nothing to "match" against a private key file (PEM body, no
    embedded key id), so this is a presence check across the three pieces
    APNs needs: team_id, key_id, and a readable .p8 at key_path.

    States:
      * `ok`        — all three present
      * `missing`   — at least one present, others missing
      * `disabled`  — nothing configured at all
    """
    team = team_id.strip() or None
    key = key_id.strip() or None
    p = _resolve_secret_path(key_path_str)
    file_ok = p is not None and p.exists()

    missing: list[str] = []
    if team is None:
        missing.append("TIGERDUCK_APNS_TEAM_ID")
    if key is None:
        missing.append("TIGERDUCK_APNS_KEY_ID")
    if not file_ok:
        missing.append(".p8 file")

    if not missing:
        return {
            "state": "ok",
            "apns_env": apns_env,
            "team_id": team,
            "key_id": key,
        }
    # `disabled` only when the operator hasn't started configuring at all —
    # avoids flagging a fresh dev checkout as broken before keys are added.
    if team is None and key is None and not file_ok:
        return {"state": "disabled", "apns_env": apns_env, "detail": "not configured"}
    return {
        "state": "missing",
        "apns_env": apns_env,
        "team_id": team,
        "key_id": key,
        "detail": "missing " + ", ".join(missing),
    }


def file_presence(path_str: str) -> dict[str, Any]:
    """Return {present, path, size_bytes?} for a secrets file. We DO NOT
    return contents — that's the whole point of file_presence vs reading."""
    if not path_str:
        return {"present": False, "path": "(unset)"}
    p = Path(path_str)
    if not p.is_absolute():
        # backend resolves relative to /app inside the container; from
        # the portal we look at the mounted host path.
        p = Path("/backend-secrets") / p.name
    try:
        if p.exists():
            return {"present": True, "path": str(p), "size_bytes": p.stat().st_size}
    except OSError:
        pass
    return {"present": False, "path": str(p)}
