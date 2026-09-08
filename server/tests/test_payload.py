"""Snapshot normalisation tests.

The Live Activity payload builders that used to live in `push/payload.py`
went with the v2 sunset — the live builder is `job_payloads.build_apns_for_job`
and its shape is pinned in `tests/push/test_job_payloads.py`. What is still
worth testing here is the piece both generations share: turning the client's
snapshot into something Swift's decoder accepts.
"""

from __future__ import annotations

from datetime import datetime, timezone

from server.push.job_payloads import build_apns_for_job

FIXED_NOW = datetime(2026, 4, 22, 2, 0, 0, tzinfo=timezone.utc)


def _job_payload(**overrides) -> dict:
    """A `live_activity_end` job payload: the client's snapshot flattened
    together with the routing keys the register endpoint adds."""
    base = {
        "kind": "live_activity_end",
        "activity_id": "inClass:slot-1",
        "source_id": "slot-1",
        "scenario": "inClass",
        "title": "計算機程式設計",
        "subtitle": "10:10-12:00",
        "locationText": "T2-401",
        "instructor": "王小明",
        "accentHex": 16743680,
        "sourceId": "slot-1",
        "countdownTarget": "2026-04-22T02:10:00+00:00",
        "progressStart": None,
    }
    base.update(overrides)
    return base


def _snapshot_of(payload: dict) -> dict:
    request = build_apns_for_job(
        payload=payload,
        channel="schedule",
        token_value="deadbeef" * 8,
        bundle_id="org.ntust.app.TigerDuck",
        now=FIXED_NOW,
    )
    return request.message["aps"]["content-state"]["snapshot"]


def test_date_fields_normalized_to_swift_reference_seconds():
    """Swift's default JSONDecoder expects Date as seconds-since-2001
    (Double). ISO8601 strings silently fail to decode in ActivityKit and
    the push vanishes. Confirm the builder converts before sending."""
    snapshot = _snapshot_of(
        _job_payload(
            countdownTarget="2026-04-22T02:00:00Z",
            progressStart="2026-04-22T01:10:00Z",
        )
    )
    assert isinstance(snapshot["countdownTarget"], float)
    assert isinstance(snapshot["progressStart"], float)
    # 2026-04-22T02:00:00Z - 2001-01-01T00:00:00Z = 798516000 seconds
    assert snapshot["countdownTarget"] == 798516000.0
    assert snapshot["progressStart"] == 798513000.0


def test_null_date_fields_stay_null():
    snapshot = _snapshot_of(
        _job_payload(countdownTarget=None, progressStart=None)
    )
    assert snapshot["countdownTarget"] is None
    assert snapshot["progressStart"] is None


def test_snapshot_input_is_not_mutated():
    """Normalization must be non-destructive so the caller's dict is safe.
    The job payload is a SQLAlchemy JSON column — mutating it in place would
    write the converted value back to the row on the next flush."""
    payload = _job_payload(countdownTarget="2026-04-22T02:00:00Z")
    before = payload["countdownTarget"]
    _ = _snapshot_of(payload)
    assert payload["countdownTarget"] == before
