"""Unit tests for access-JWT and refresh-token primitives."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt as pyjwt
import pytest

from server.auth.tokens import (
    InvalidAccessToken,
    decode_access_token,
    generate_refresh_token,
    hash_refresh_token,
    issue_access_token,
)

SECRET = "unit-test-secret-0123456789abcdef0123456789abcdef"


def test_access_token_round_trip() -> None:
    token = issue_access_token(
        SECRET,
        user_id="0c8e3f44-1111-2222-3333-444455556666",
        session_id=7,
        device_id="aaaabbbb-1111-2222-3333-444455556666",
        ttl_seconds=900,
    )
    claims = decode_access_token(SECRET, token)
    assert claims.user_id == "0c8e3f44-1111-2222-3333-444455556666"
    assert claims.session_id == 7
    assert claims.device_id == "aaaabbbb-1111-2222-3333-444455556666"


def test_access_token_device_optional() -> None:
    token = issue_access_token(
        SECRET, user_id="u", session_id=1, device_id=None, ttl_seconds=900
    )
    assert decode_access_token(SECRET, token).device_id is None


def test_expired_token_raises() -> None:
    past = datetime.now(UTC) - timedelta(hours=1)
    token = issue_access_token(
        SECRET, user_id="u", session_id=1, device_id=None, ttl_seconds=60, now=past
    )
    with pytest.raises(InvalidAccessToken):
        decode_access_token(SECRET, token)


def test_wrong_secret_raises() -> None:
    token = issue_access_token(
        SECRET, user_id="u", session_id=1, device_id=None, ttl_seconds=900
    )
    with pytest.raises(InvalidAccessToken):
        decode_access_token("other-secret-0123456789abcdef0123456789abcdef", token)


def test_token_without_token_use_claim_raises() -> None:
    rogue = pyjwt.encode(
        {"sub": "u", "sid": 1, "exp": datetime.now(UTC) + timedelta(minutes=5)},
        SECRET,
        algorithm="HS256",
    )
    with pytest.raises(InvalidAccessToken):
        decode_access_token(SECRET, rogue)


def test_garbage_token_raises() -> None:
    with pytest.raises(InvalidAccessToken):
        decode_access_token(SECRET, "not-a-jwt")


def test_refresh_token_generation_is_random_and_long() -> None:
    a, b = generate_refresh_token(), generate_refresh_token()
    assert a != b
    assert len(a) >= 64


def test_refresh_hash_deterministic_and_keyed() -> None:
    token = generate_refresh_token()
    h1 = hash_refresh_token("key-a", token)
    assert h1 == hash_refresh_token("key-a", token)
    assert len(h1) == 64
    assert int(h1, 16) >= 0  # hex
    assert h1 != hash_refresh_token("key-b", token)
