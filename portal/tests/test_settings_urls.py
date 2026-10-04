"""The status page's default URLs follow the rule _compose-files.sh uses.

start.sh loads the dev override, which publishes localhost ports, only when
TIGERDUCK_ENV is exactly "development"; anything else, a missing key
included, comes up prod-shaped on proxy-net. The portal must show the same
URLs start.sh prints, or the status page points at ports nothing serves.
"""
from __future__ import annotations

import pytest

from app.config import Settings


@pytest.fixture(autouse=True)
def _no_url_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TIGERDUCK_BACKEND_PUBLIC_URL", raising=False)
    monkeypatch.delenv("TIGERDUCK_PORTAL_PUBLIC_URL", raising=False)


def test_development_points_at_the_published_localhost_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TIGERDUCK_ENV", "development")

    settings = Settings.from_env()

    assert settings.backend_public_url == "http://localhost:40000"
    assert settings.portal_public_url == "http://localhost:40010"


def test_a_missing_env_points_at_proxy_net_like_start_sh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TIGERDUCK_ENV", raising=False)

    settings = Settings.from_env()

    assert settings.backend_public_url == "http://tigerduck-internal:40000"
    assert settings.portal_public_url == "http://tigerduck-portal:40010"
    # The mode itself keeps the backend's fallback.
    assert settings.env == "development"


def test_production_points_at_proxy_net(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIGERDUCK_ENV", "production")

    settings = Settings.from_env()

    assert settings.backend_public_url == "http://tigerduck-internal:40000"
    assert settings.portal_public_url == "http://tigerduck-portal:40010"
