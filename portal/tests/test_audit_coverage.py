"""Every portal route that changes state writes an audit row.

Reads the route modules rather than running them: the routes need a database
these tests do not have. A new write route either calls `audit()` or joins
the list of read-only POSTs below.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROUTES = Path(__file__).resolve().parents[1] / "app" / "routes"
WRITE_METHODS = {"post", "put", "patch", "delete"}
# POSTs that only read.
READ_ONLY = {("custom_push.py", "preview")}


def _write_routes() -> list[tuple[str, str, str]]:
    found = []
    for path in sorted(ROUTES.rglob("*.py")):
        source = path.read_text()
        for node in ast.parse(source).body:
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            for deco in node.decorator_list:
                if (
                    isinstance(deco, ast.Call)
                    and isinstance(deco.func, ast.Attribute)
                    and deco.func.attr in WRITE_METHODS
                ):
                    body = ast.get_source_segment(source, node)
                    found.append((path.name, node.name, body))
    return found


def test_every_write_route_calls_audit() -> None:
    routes = _write_routes()
    missing = [
        (file, name)
        for file, name, body in routes
        if (file, name) not in READ_ONLY and "await audit(" not in body
    ]

    assert len(routes) > 20
    assert not missing, f"write routes without an audit() call: {missing}"
