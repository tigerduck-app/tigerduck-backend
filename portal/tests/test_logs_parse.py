"""Log lines split into time, level, logger, event and fields for the Logs table."""
from __future__ import annotations

from app.logs import parse_line

TS = "2026-10-29T01:02:03.123456789Z"


def test_backend_json_line() -> None:
    line = parse_line(f'{TS} {{"event": "push.sent", "level": "info", "timestamp": "x", "job_id": 5, "ok": true}}')
    assert line == {
        "ts": "2026-10-29T01:02:03.123Z",
        "level": "info",
        "logger": "",
        "event": "push.sent",
        "kv": [["job_id", "5"], ["ok", "true"]],
        "raw": '{"event": "push.sent", "level": "info", "timestamp": "x", "job_id": 5, "ok": true}',
    }


def test_stdlib_and_uvicorn_lines() -> None:
    lib = parse_line(f"{TS} WARNING:apscheduler.scheduler: Run time of job was missed")
    assert (lib["level"], lib["logger"], lib["event"]) == (
        "warning", "apscheduler.scheduler", "Run time of job was missed"
    )
    access = parse_line(f'{TS} INFO:     172.18.0.1:5 - "GET /api/status HTTP/1.1" 200 OK')
    assert (access["level"], access["logger"], access["event"]) == (
        "info", "uvicorn", '172.18.0.1:5 - "GET /api/status HTTP/1.1" 200 OK'
    )


def test_postgres_line() -> None:
    line = parse_line(f"{TS} 2026-10-29 01:02:03.123 UTC [61] FATAL:  role \"x\" does not exist")
    assert (line["level"], line["logger"], line["event"]) == (
        "error", "postgres", 'role "x" does not exist'
    )


def test_console_renderer_line() -> None:
    line = parse_line(f"{TS} 2026-10-29T01:02:03Z [warning  ] moodle.sync.slow   duration_ms=9000 name='a b'")
    assert (line["level"], line["event"], line["kv"]) == (
        "warning", "moodle.sync.slow", [["duration_ms", "9000"], ["name", "'a b'"]]
    )


def test_a_line_without_a_level_keeps_its_text() -> None:
    line = parse_line("Traceback (most recent call last):")
    assert line == {
        "ts": None,
        "level": None,
        "logger": "",
        "event": "Traceback (most recent call last):",
        "kv": [],
        "raw": "Traceback (most recent call last):",
    }
