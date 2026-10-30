"""The backend image runs as an unprivileged user and is built with a uv
pinned by version and digest."""

from __future__ import annotations

import re
from pathlib import Path

DOCKERFILE = (Path(__file__).resolve().parents[2] / "Dockerfile").read_text()


def test_the_runtime_stage_runs_as_a_user_other_than_root() -> None:
    runtime = DOCKERFILE.rsplit("\nFROM ", 1)[1]
    users = re.findall(r"^USER\s+(\S+)", runtime, re.MULTILINE)
    assert users, "the runtime stage sets no USER"
    assert users[-1].split(":")[0] not in {"root", "0"}


def test_uv_is_pinned_by_version_and_digest() -> None:
    images = re.findall(r"ghcr\.io/astral-sh/uv\S*", DOCKERFILE)
    assert images
    for image in images:
        assert re.fullmatch(r"ghcr\.io/astral-sh/uv:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}", image)
