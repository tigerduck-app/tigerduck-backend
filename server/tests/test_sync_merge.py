"""Unit tests for per-field merge with clock-skew clamping."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from server.sync.merge import apply_field, clamp_ts

NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)
DEVICE = uuid.uuid4()


@dataclass
class FakeOverride:
    color_hex: str | None = None
    color_hex_updated_at: datetime | None = None
    color_hex_device_id: uuid.UUID | None = field(default=None)


def test_clamp_passes_past_timestamps() -> None:
    past = NOW - timedelta(hours=3)
    assert clamp_ts(past, NOW) == past


def test_clamp_caps_future_timestamps() -> None:
    future = NOW + timedelta(days=365)
    assert clamp_ts(future, NOW) == NOW


def test_apply_field_sets_when_no_previous_timestamp() -> None:
    entity = FakeOverride()
    changed = apply_field(
        entity, "color_hex", "#FF8800", client_ts=NOW, device_id=DEVICE, now=NOW
    )
    assert changed is True
    assert entity.color_hex == "#FF8800"
    assert entity.color_hex_updated_at == NOW
    assert entity.color_hex_device_id == DEVICE


def test_apply_field_newer_wins() -> None:
    entity = FakeOverride(
        color_hex="#000000", color_hex_updated_at=NOW - timedelta(hours=1)
    )
    changed = apply_field(
        entity, "color_hex", "#FF8800", client_ts=NOW, device_id=DEVICE, now=NOW
    )
    assert changed is True
    assert entity.color_hex == "#FF8800"


def test_apply_field_older_loses() -> None:
    entity = FakeOverride(color_hex="#000000", color_hex_updated_at=NOW)
    changed = apply_field(
        entity,
        "color_hex",
        "#FF8800",
        client_ts=NOW - timedelta(hours=1),
        device_id=DEVICE,
        now=NOW,
    )
    assert changed is False
    assert entity.color_hex == "#000000"


def test_future_timestamp_cannot_poison_merge() -> None:
    # A device with a clock a year ahead writes a value; its timestamp
    # gets clamped to "now", so other devices' future edits with honest
    # timestamps still win.
    entity = FakeOverride()
    apply_field(
        entity,
        "color_hex",
        "#BAD",
        client_ts=NOW + timedelta(days=365),
        device_id=DEVICE,
        now=NOW,
    )
    assert entity.color_hex_updated_at == NOW  # clamped, not a year ahead

    later = NOW + timedelta(minutes=5)
    changed = apply_field(
        entity,
        "color_hex",
        "#GOOD",
        client_ts=later,
        device_id=DEVICE,
        now=later,
    )
    assert changed is True
    assert entity.color_hex == "#GOOD"


def test_equal_timestamp_does_not_overwrite() -> None:
    entity = FakeOverride(color_hex="#000000", color_hex_updated_at=NOW)
    changed = apply_field(
        entity, "color_hex", "#FF8800", client_ts=NOW, device_id=DEVICE, now=NOW
    )
    assert changed is False
