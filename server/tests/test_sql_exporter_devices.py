"""The sql-exporter's anonymous device gauge
(`monitoring/sql-exporter/collectors/devices.collector.yml`), its query run on
the test database as the exporter runs it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from sqlalchemy import text

from server.models import DeviceRegistration

pytestmark = pytest.mark.asyncio(loop_scope="session")

COLLECTOR = (
    Path(__file__).resolve().parents[2]
    / "monitoring" / "sql-exporter" / "collectors" / "devices.collector.yml"
)


def metric(name: str) -> dict:
    metrics = yaml.safe_load(COLLECTOR.read_text())["metrics"]
    return {m["metric_name"]: m for m in metrics}[name]


async def test_anonymous_devices_counts_recent_announces_by_platform_and_secret(
    db_session,
) -> None:
    gauge = metric("tigerduck_anonymous_devices")
    now = datetime.now(UTC)
    rows = [
        ("apple-bound", "apple", "f" * 64, now),
        ("apple-none-1", "apple", None, now),
        ("apple-none-2", "apple", None, now - timedelta(days=13)),
        ("android-bound", "android", "e" * 64, now),
        ("apple-quiet", "apple", "d" * 64, now - timedelta(days=15)),
    ]
    db_session.add_all(
        DeviceRegistration(
            device_id=device_id, user_id=f"anon-{device_id}", platform=platform,
            pts_token_hex="", bundle_id="", attrs_type="", apns_env="",
            secret_hash=secret_hash, updated_at=updated_at,
        )
        for device_id, platform, secret_hash, updated_at in rows
    )
    await db_session.commit()

    result = (await db_session.execute(text(gauge["query"]))).mappings().all()

    assert gauge["key_labels"] == ["platform", "secret_bound"]
    assert {(r["platform"], r["secret_bound"]): r["count"] for r in result} == {
        ("apple", "true"): 1,
        ("apple", "false"): 2,
        ("android", "true"): 1,
    }
