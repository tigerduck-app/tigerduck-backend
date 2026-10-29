"""Every portal route that changes state, shows one student's data or lists
many students or devices writes an audit row.

Reads the route modules rather than running them: the routes need a database
these tests do not have. A new route has to land in one of the sets below.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROUTES = Path(__file__).resolve().parents[1] / "app" / "routes"
WRITE_METHODS = {"post", "put", "patch", "delete"}
# POSTs that only read.
READ_ONLY_WRITES = {("custom_push.py", "preview")}
# Writes the backend's admin listener records in admin_audit_log itself, in
# the change's transaction, with the operator `_forward` names in X-Admin-Actor.
BACKEND_AUDITED_WRITES = {
    ("links.py", "create_link"),
    ("links.py", "test_link"),
    ("links.py", "update_link"),
    *(
        ("tls_pins.py", name)
        for name in (
            "add_pin", "remove_pin", "set_monitored_hosts", "extend_expiry",
            "set_auto_publish", "publish", "resign",
        )
    ),
}
# GETs that return one student's data (user.view) or many students' ids or
# devices (user.list).
PERSONAL_READS = {
    ("audit.py", "list_audit"),
    ("device_lists.py", "list_members"),
    ("devices.py", "list_devices"),
    ("moodle/inspection.py", "list_moodle_students"),
    ("moodle/inspection.py", "sync_courses"),
    ("moodle/inspection.py", "sync_events"),
    ("moodle/jobs.py", "list_moodle_jobs"),
    ("moodle/logs.py", "sync_logs"),
    ("moodle/status.py", "moodle_stats"),
}
# GETs that return neither.
OTHER_READS = {
    ("academic_calendar.py", "get_calendar"),
    ("announcement.py", "get_bulletin"),
    ("announcement.py", "list_bulletins"),
    ("announcement.py", "taxonomy"),
    ("custom_push.py", "recent"),
    ("device_lists.py", "get_list"),
    ("device_lists.py", "list_lists"),
    ("links.py", "list_links"),
    ("logs.py", "data"),
    ("logs.py", "tabs"),
    ("moodle/status.py", "moodle_status"),
    ("status.py", "env_info"),
    ("status.py", "health"),
    ("status.py", "ready"),
    ("status.py", "status_payload"),
    ("status.py", "version_info"),
    ("tls_pins.py", "get_tls_pins"),
}


def _routes() -> list[tuple[str, str, str, str]]:
    """(module path, function, HTTP method, source) for every route."""
    found = []
    for path in sorted(ROUTES.rglob("*.py")):
        source = path.read_text()
        module = path.relative_to(ROUTES).as_posix()
        for node in ast.parse(source).body:
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            for deco in node.decorator_list:
                if isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute):
                    body = ast.get_source_segment(source, node)
                    found.append((module, node.name, deco.func.attr, body))
    return found


def test_every_write_route_calls_audit() -> None:
    writes = [r for r in _routes() if r[2] in WRITE_METHODS]
    missing = [
        (module, name)
        for module, name, _, body in writes
        if (module, name) not in READ_ONLY_WRITES | BACKEND_AUDITED_WRITES
        and "await audit(" not in body
    ]
    unforwarded = [
        (module, name)
        for module, name, _, body in writes
        if (module, name) in BACKEND_AUDITED_WRITES and "await _forward(" not in body
    ]

    assert len(writes) > 20
    assert not missing, f"write routes without an audit() call: {missing}"
    assert not unforwarded, f"backend-audited routes that skip _forward: {unforwarded}"


def test_every_read_route_is_classified_and_personal_reads_call_audit() -> None:
    reads = {(m, n): body for m, n, method, body in _routes() if method == "get"}

    unclassified = set(reads) - PERSONAL_READS - OTHER_READS
    assert not unclassified, f"add these GET routes to PERSONAL_READS or OTHER_READS: {unclassified}"
    missing = [key for key in PERSONAL_READS if "await audit(" not in reads[key]]
    assert not missing, f"personal reads without an audit() call: {missing}"
