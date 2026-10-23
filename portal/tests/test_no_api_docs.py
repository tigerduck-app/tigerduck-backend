"""The API schema and docs pages are not served: they sit outside /api,
where no Access token is checked, and would list every operator route."""
from __future__ import annotations

import httpx
import pytest

from app.main import app

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("path", ["/openapi.json", "/docs", "/redoc"])
async def test_docs_and_schema_are_not_served(path: str) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://portal") as c:
        r = await c.get(path)

    # The SPA fallback answers instead: its shell, or 503 without a build.
    assert "/api/" not in r.text
    assert "swagger" not in r.text.lower()
    assert "redoc" not in r.text.lower()
