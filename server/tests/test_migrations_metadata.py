"""Alembic must be able to see every table the models declare.

`server/migrations/env.py` hands Alembic `Base.metadata`, and Alembic writes a
`drop_table` for every table present in the database but absent from that
metadata. A model module nothing imports is therefore not a harmless oversight:
it turns the next `alembic revision --autogenerate` into a migration that
deletes production tables.

That is not hypothetical. Four tables -- `academic_holidays`, `semester_terms`,
`system_settings`, `user_holiday_overrides` -- were invisible to Alembic for
months because `env.py` imported only two model modules, and every autogenerate
run in that window proposed dropping all four.

These tests run in a subprocess because the check is about what a *fresh*
interpreter sees. Importing anything here first would contaminate the result:
by the time the rest of the suite has run, every model module is loaded and the
gap the test exists to catch has closed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Mirrors the import block in server/migrations/env.py. Keep the two in step:
# if you add an import there, add it here, and the second test below will tell
# you when env.py has fallen behind the models.
_ENV_PY_IMPORTS = """
from server.db import Base
from server import models
from server.bulletins import models as _bulletin_models
from server.academic_calendar import models as _academic_calendar_models
from server import system_settings as _system_settings_models
"""


def _run(script: str) -> set[str]:
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return set(result.stdout.split())


def test_env_py_imports_reach_every_declared_table() -> None:
    """Loading exactly what env.py loads must populate the whole metadata."""
    via_env_py = _run(
        _ENV_PY_IMPORTS + "\nprint('\\n'.join(sorted(Base.metadata.tables)))\n"
    )

    everything = _run(
        _ENV_PY_IMPORTS
        + """
import importlib
import pkgutil

import server

for mod in pkgutil.walk_packages(server.__path__, "server."):
    if ".migrations." in mod.name or mod.name.endswith(".tests"):
        continue
    try:
        importlib.import_module(mod.name)
    except Exception:
        continue

print('\\n'.join(sorted(Base.metadata.tables)))
"""
    )

    missing = everything - via_env_py
    assert not missing, (
        "These tables are declared by models that env.py's import chain never "
        f"reaches, so alembic autogenerate will propose dropping them: "
        f"{sorted(missing)}. Add the missing module import to "
        "server/migrations/env.py (and to _ENV_PY_IMPORTS in this file)."
    )


def test_this_test_mirrors_env_py() -> None:
    """The list above is a copy, so guard against it drifting from the original."""
    env_py = (REPO_ROOT / "server" / "migrations" / "env.py").read_text()
    for line in _ENV_PY_IMPORTS.strip().splitlines():
        module = line.split(" import ")[0].removeprefix("from ").strip()
        imported = line.split(" import ")[1].split(" as ")[0].strip()
        needle = f"from {module} import {imported}"
        assert needle in env_py, (
            f"{needle!r} is in this test's copy of env.py's imports but not in "
            "server/migrations/env.py itself. One of the two is out of date."
        )
