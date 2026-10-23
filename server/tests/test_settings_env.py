"""An unset TIGERDUCK_ENV is production, as it is for ./start.sh."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from server.config import Settings


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv("TIGERDUCK_ENV", raising=False)
    monkeypatch.delenv("TIGERDUCK_API_SHARED_SECRET", raising=False)


def test_an_unset_env_is_production():
    assert Settings(_env_file=None, api_shared_secret="s").env == "production"


def test_an_unset_env_refuses_to_start_without_a_shared_secret():
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
