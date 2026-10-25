"""Unit tests for per-field merge with clock-skew clamping."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from server.sync.merge import (
    MAX_CLOCK_SKEW,
    OLDEST,
    apply_course_name,
    apply_field,
    clamp_ts,
    edit_time,
    from_clock,
    set_locale_less_name,
    to_clock,
)

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
    assert clamp_ts(future, NOW, skew=MAX_CLOCK_SKEW) == NOW + timedelta(seconds=5)


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
    # Clamped to the skew allowance, not a year ahead.
    assert entity.color_hex_updated_at == NOW + MAX_CLOCK_SKEW

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


def test_clock_values_are_exact_microseconds() -> None:
    stamp = datetime(2026, 10, 25, 0, 39, 59, 123456, tzinfo=UTC)
    assert to_clock(stamp) == 1792888799123456
    assert from_clock(1792888799123456) == stamp
    assert from_clock(0) == OLDEST


def test_edit_time_is_arrival_without_a_stamp_and_clamped_with_one() -> None:
    assert edit_time(None, NOW) == NOW
    week_ago = NOW - timedelta(days=7)
    assert edit_time(to_clock(week_ago), NOW) == week_ago
    assert edit_time(2**63 - 1, NOW) == NOW + MAX_CLOCK_SKEW


def test_same_value_raises_the_stamp_without_a_change() -> None:
    entity = FakeOverride(color_hex="#FF0000", color_hex_updated_at=NOW)
    later = NOW + timedelta(minutes=5)
    changed = apply_field(
        entity, "color_hex", "#FF0000", client_ts=later, device_id=DEVICE, now=later
    )
    assert changed is False
    assert entity.color_hex_updated_at == later

    # The raised stamp keeps an edit made between the two from winning.
    between = NOW + timedelta(minutes=3)
    changed = apply_field(
        entity, "color_hex", "#0000FF", client_ts=between, device_id=DEVICE, now=later
    )
    assert changed is False
    assert entity.color_hex == "#FF0000"


def test_a_field_without_a_stamp_takes_the_oldest_value() -> None:
    entity = FakeOverride()
    changed = apply_field(
        entity, "color_hex", "#FF8800", client_ts=OLDEST, device_id=DEVICE, now=NOW
    )
    assert changed is True
    assert entity.color_hex_updated_at == OLDEST

    changed = apply_field(
        entity, "color_hex", "#000000", client_ts=OLDEST, device_id=DEVICE, now=NOW
    )
    assert changed is False
    assert entity.color_hex == "#FF8800"


@dataclass
class FakeCourseOverride:
    custom_names: dict = field(default_factory=dict)
    custom_names_updated_at: dict = field(default_factory=dict)
    custom_name_updated_at: datetime | None = None
    custom_name_device_id: uuid.UUID | None = None


def test_each_locale_name_resolves_on_its_own() -> None:
    override = FakeCourseOverride()
    assert apply_course_name(
        override, "zh", "作業系統", client_ts=NOW, device_id=DEVICE, now=NOW
    )
    earlier = NOW - timedelta(minutes=1)
    assert apply_course_name(
        override, "en", "OS", client_ts=earlier, device_id=DEVICE, now=NOW
    )
    assert override.custom_names == {"zh": "作業系統", "en": "OS"}
    assert override.custom_names_updated_at == {
        "zh": to_clock(NOW),
        "en": to_clock(earlier),
    }
    # The locale-blind stamp keeps the latest of them.
    assert override.custom_name_updated_at == NOW

    assert not apply_course_name(
        override, "zh", "OS", client_ts=earlier, device_id=DEVICE, now=NOW
    )
    assert override.custom_names["zh"] == "作業系統"


def test_a_name_without_a_locale_is_both_locales() -> None:
    override = FakeCourseOverride()
    assert set_locale_less_name(
        override, "作業系統", client_ts=NOW, device_id=DEVICE, now=NOW
    )
    assert override.custom_names == {"zh": "作業系統", "en": "作業系統"}
    assert set_locale_less_name(
        override, None, client_ts=NOW + timedelta(seconds=1), device_id=DEVICE, now=NOW
    )
    assert override.custom_names == {"zh": "", "en": ""}
