"""The OpenAPI schema and the docs pages built on it are served in
development only: in production they would list every route to anyone."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from server import main
from server.config import Settings
from server.tests.conftest import PRODUCTION_SECRETS

DOC_PATHS = ("/openapi.json", "/docs", "/redoc")


async def _statuses(settings: Settings, monkeypatch) -> dict[str, int]:
    # Production logging is JSON for the whole process; keep it out of other tests.
    monkeypatch.setattr(main, "configure_logging", lambda _settings: None)
    app = main.create_app(settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://api") as client:
        return {path: (await client.get(path)).status_code for path in DOC_PATHS}


@pytest.mark.asyncio
async def test_production_serves_no_schema_or_docs(monkeypatch):
    settings = Settings(_env_file=None, env="production", **PRODUCTION_SECRETS)
    assert await _statuses(settings, monkeypatch) == dict.fromkeys(DOC_PATHS, 404)


@pytest.mark.asyncio
async def test_development_serves_them(monkeypatch):
    settings = Settings(_env_file=None, env="development")
    assert await _statuses(settings, monkeypatch) == dict.fromkeys(DOC_PATHS, 200)
