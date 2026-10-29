"""Log retrieval — docker engine logs over the mounted UDS.

We pull `/containers/{name}/logs?stdout=1&stderr=1&tail=N&timestamps=1`
and demultiplex the engine's framed binary stream (8-byte header per
frame: 1B stream id, 3B zero, 4B big-endian length, then payload). The
demux is required whenever the container was started without a TTY,
which is the default for our compose services.

Filtering for the android / apple sections is done in-process by
substring-matching backend log lines. This keeps the wire format simple
(just text) and lets the same fetched buffer feed multiple sections.
"""
from __future__ import annotations

import json
import re
import struct
from typing import Any

import httpx

# Strip ANSI CSI / SGR sequences emitted by structlog's ConsoleRenderer
# (and anything else that thinks it's writing to a real terminal). The
# escape byte itself (ESC = 0x1b) becomes invisible in the browser, so
# without this the user sees the bracket-codes like `[36m` next to every
# key=value pair.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)

from .status import DOCKER_SOCK

# Cap how much we pull per container so the rendered page stays bounded
# even when something is in a crash loop. The UI offers a `tail` query
# param to widen on demand.
DEFAULT_TAIL = 500
MAX_TAIL = 5000


def _demux(blob: bytes) -> str:
    """Demultiplex a non-TTY docker logs stream into plain text.

    The engine prefixes every chunk with `[stream:1B][\\x00\\x00\\x00][len:4B BE]`.
    We treat stdout and stderr as one merged stream (preserving original
    interleaving) — the UI doesn't need to distinguish them.
    """
    out = bytearray()
    i = 0
    n = len(blob)
    while i + 8 <= n:
        # The first byte is the stream id; we don't differentiate, so
        # we only need the length out of the header.
        length = struct.unpack(">I", blob[i + 4 : i + 8])[0]
        i += 8
        end = i + length
        if end > n:
            # Truncated frame — append what's left and bail. The next
            # poll will pick up the rest.
            out.extend(blob[i:n])
            break
        out.extend(blob[i:end])
        i = end
    if not out and blob:
        # Some engines/setups still hand back raw text (TTY-enabled
        # containers, or older API versions). Fall back gracefully so a
        # display happens regardless.
        return _strip_ansi(blob.decode("utf-8", errors="replace"))
    return _strip_ansi(out.decode("utf-8", errors="replace"))


async def container_logs(
    name: str,
    tail: int = DEFAULT_TAIL,
    timeout_s: float = 5.0,
) -> dict[str, Any]:
    """Return `{ok, text, detail?}` for the named container.

    `ok=False` covers every failure mode (socket missing, container
    missing, engine error) so the template renders a single error
    message instead of having to special-case each.
    """
    tail = max(1, min(int(tail), MAX_TAIL))
    transport = httpx.AsyncHTTPTransport(uds=DOCKER_SOCK)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://docker",
            timeout=timeout_s,
        ) as client:
            r = await client.get(
                f"/containers/{name}/logs",
                params={
                    "stdout": "1",
                    "stderr": "1",
                    "tail": str(tail),
                    "timestamps": "1",
                },
            )
    except httpx.HTTPError as exc:
        return {"ok": False, "text": "", "detail": f"{type(exc).__name__}: {exc}"}
    finally:
        await transport.aclose()

    if r.status_code == 404:
        return {"ok": False, "text": "", "detail": f"container {name!r} not found"}
    if r.status_code >= 400:
        return {"ok": False, "text": "", "detail": f"engine HTTP {r.status_code}"}

    return {"ok": True, "text": _demux(r.content)}


def filter_lines(text: str, needles: list[str]) -> str:
    """Case-insensitive substring filter. Empty `needles` returns input
    unchanged so the same helper can serve 'show everything' too."""
    if not needles:
        return text
    lowered = [n.lower() for n in needles if n]
    if not lowered:
        return text
    kept: list[str] = []
    for line in text.splitlines():
        ll = line.lower()
        if any(n in ll for n in lowered):
            kept.append(line)
    return "\n".join(kept)


# Keyword sets for the topical sections. Loose on purpose — better to
# include a borderline-relevant line than to silently drop it; the
# per-section search bar narrows further.
ANDROID_NEEDLES = ["fcm", "android", "firebase"]
APPLE_NEEDLES = ["apns", "apple", "live_activity", "live-activity", "pts"]
# Bulletin / announcement-related lines. Covers the structlog event keys
# (`bulletins.*`), the module name (`server.bulletins`, `server.routes.bulletins`),
# and the human word so admin POST/PATCH paths surface alongside scraper
# + dispatcher activity.
ANNOUNCEMENT_NEEDLES = ["bulletin", "announcement"]


# Docker's own receive time leads every line (`timestamps=1` above).
_DOCKER_TS = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?Z (.*)$")
# The backend's stdlib loggers (server/logging_setup.py) and uvicorn's.
_STDLIB = re.compile(r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL):([\w.]+): ?(.*)$")
_UVICORN = re.compile(r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL):\s+(.*)$")
# structlog's ConsoleRenderer, which the backend uses in development.
_CONSOLE = re.compile(r"^\S+ \[(\w+)\s*\] (\S+)\s*(.*)$")
_CONSOLE_KV = re.compile(r"([\w.-]+)=('[^']*'|\"[^\"]*\"|\S+)")
_POSTGRES = re.compile(
    r"^\d{4}-\d\d-\d\d [\d:.]+ \w+ \[\d+\] (DEBUG\d|LOG|INFO|NOTICE|WARNING|ERROR|FATAL|PANIC):\s+(.*)$"
)
_LEVELS = {
    "debug": "debug",
    "info": "info",
    "log": "info",
    "notice": "info",
    "warn": "warning",
    "warning": "warning",
    "error": "error",
    "exception": "error",
    "critical": "error",
    "fatal": "error",
    "panic": "error",
}


def _level(raw: str) -> str | None:
    return _LEVELS.get(raw.lower().rstrip("0123456789"))


def _json_line(text: str) -> dict[str, Any] | None:
    try:
        body = json.loads(text)
    except ValueError:
        return None
    if not isinstance(body, dict) or "event" not in body:
        return None
    kv = [
        [k, v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)]
        for k, v in body.items()
        if k not in ("event", "level", "timestamp", "logger")
    ]
    return {
        "level": _level(str(body.get("level", ""))),
        "logger": str(body.get("logger") or ""),
        "event": str(body["event"]),
        "kv": kv,
    }


def _fields(text: str) -> dict[str, Any]:
    if text.startswith("{") and (parsed := _json_line(text)):
        return parsed
    if m := _STDLIB.match(text):
        return {"level": _level(m[1]), "logger": m[2], "event": m[3], "kv": []}
    if m := _UVICORN.match(text):
        return {"level": _level(m[1]), "logger": "uvicorn", "event": m[2], "kv": []}
    if m := _POSTGRES.match(text):
        return {"level": _level(m[1]), "logger": "postgres", "event": m[2], "kv": []}
    if (m := _CONSOLE.match(text)) and (level := _level(m[1])):
        kv = [[k, v] for k, v in _CONSOLE_KV.findall(m[3])]
        return {"level": level, "logger": "", "event": m[2], "kv": kv}
    return {"level": None, "logger": "", "event": text, "kv": []}


def parse_line(line: str) -> dict[str, Any]:
    """One log line as the Logs table shows it: `ts` in milliseconds,
    `level` one of error, warning, info and debug, or None."""
    ts = None
    text = line
    if m := _DOCKER_TS.match(line):
        ts = f"{m[1]}.{(m[2] or '').ljust(3, '0')[:3]}Z"
        text = m[3]
    return {"ts": ts, **_fields(text), "raw": text}
