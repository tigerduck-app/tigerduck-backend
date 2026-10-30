"""Startup refuses a configured file it cannot read, naming its setting,
rather than run with the feature behind it quietly off."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from server import main
from server.config import Settings, require_readable_files

FILE_SETTINGS = {
    "apns_key_path": "TIGERDUCK_APNS_KEY_PATH",
    "fcm_credentials_path": "TIGERDUCK_FCM_CREDENTIALS_PATH",
    "tls_pin_signing_key_path": "TIGERDUCK_TLS_PIN_SIGNING_KEY_PATH",
    "bulletin_ca_bundle": "TIGERDUCK_BULLETIN_CA_BUNDLE",
}


def _settings(tmp_path: Path, **files: Path) -> Settings:
    missing = {
        "apns_key_path": tmp_path / "missing.p8",
        "fcm_credentials_path": tmp_path / "missing.json",
        "tls_pin_signing_key_path": None,
        "bulletin_ca_bundle": None,
    }
    return Settings(_env_file=None, env="development", **{**missing, **files})


@pytest.fixture
def unreadable(tmp_path: Path) -> Path:
    if os.geteuid() == 0:
        pytest.skip("root reads any file")
    path = tmp_path / "unreadable"
    path.write_bytes(b"key")
    path.chmod(0)
    return path


@pytest.mark.parametrize(("field", "name"), FILE_SETTINGS.items())
def test_an_unreadable_file_fails_naming_its_setting(tmp_path, unreadable, field, name):
    with pytest.raises(RuntimeError, match=f"^{name} names .* cannot read"):
        require_readable_files(_settings(tmp_path, **{field: unreadable}))


def test_missing_key_files_leave_their_features_off(tmp_path):
    require_readable_files(_settings(tmp_path, tls_pin_signing_key_path=tmp_path / "k.pem"))


def test_a_missing_ca_bundle_fails_since_it_was_asked_for(tmp_path):
    with pytest.raises(RuntimeError, match="^TIGERDUCK_BULLETIN_CA_BUNDLE names .* does not exist"):
        require_readable_files(_settings(tmp_path, bulletin_ca_bundle=tmp_path / "ca.pem"))


def test_readable_files_pass(tmp_path):
    files = {}
    for field in FILE_SETTINGS:
        files[field] = tmp_path / field
        files[field].write_bytes(b"x")
    require_readable_files(_settings(tmp_path, **files))


async def test_the_app_does_not_start_with_one(tmp_path, unreadable, monkeypatch):
    monkeypatch.setattr(main, "configure_logging", lambda _settings: None)
    app = main.create_app(_settings(tmp_path, apns_key_path=unreadable))

    with pytest.raises(RuntimeError, match="^TIGERDUCK_APNS_KEY_PATH"):
        async with main.lifespan(app):
            pass
