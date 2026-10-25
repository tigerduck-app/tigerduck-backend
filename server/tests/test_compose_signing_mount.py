"""Only the backend mounts the TLS pin signing key's folder. The portal
mounts server/secrets and has the Docker socket, so it must never see it."""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SIGNING = REPO_ROOT / "server" / "signing"
COMPOSE_FILES = ("docker-compose.yml", "docker-compose.dev.yml")


class _ComposeLoader(yaml.SafeLoader):
    """Reads compose's merge tags (`!override`, `!reset`) as plain values."""


def _untagged(loader: yaml.SafeLoader, _suffix: str, node: yaml.Node):
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_scalar(node)


_ComposeLoader.add_multi_constructor("!", _untagged)


def _bind_mounts(service: dict) -> list[tuple[Path, bool]]:
    """(host source, read only) of each bind mount; named volumes are left out."""
    mounts = []
    for volume in service.get("volumes", []):
        if isinstance(volume, str):
            source, _, rest = volume.partition(":")
            read_only = rest.endswith(":ro")
        else:
            source, read_only = volume.get("source", ""), bool(volume.get("read_only"))
        if source.startswith((".", "/", "~")):
            mounts.append(((REPO_ROOT / source).resolve(), read_only))
    return mounts


def _mounts_of_signing() -> dict[tuple[str, str], bool]:
    found = {}
    for name in COMPOSE_FILES:
        compose = yaml.load((REPO_ROOT / name).read_text(), Loader=_ComposeLoader)
        services = compose.get("services") or {}
        for service, spec in services.items():
            for source, read_only in _bind_mounts(spec or {}):
                if SIGNING == source or SIGNING.is_relative_to(source):
                    found[(name, service)] = read_only
    return found


def test_only_the_backend_mounts_the_signing_key_and_read_only() -> None:
    assert _mounts_of_signing() == {("docker-compose.yml", "backend"): True}


def test_the_signing_key_folder_is_never_in_the_image_or_in_git() -> None:
    assert "server/signing/" in (REPO_ROOT / ".dockerignore").read_text().splitlines()
    assert "server/signing/" in (REPO_ROOT / ".gitignore").read_text().splitlines()
