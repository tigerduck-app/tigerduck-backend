"""The migrations must form one line. Two migrations on the same parent
leave Alembic two heads, and `alembic upgrade head`, which the backend
runs as it starts (`entrypoint.sh`), refuses to choose between them."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_the_migrations_have_one_head() -> None:
    # Imported here: Alembic logs its plugins as it loads, into the stdout
    # test_migrations_metadata reads after it imports every module.
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    assert len(script.get_heads()) == 1, script.get_heads()
