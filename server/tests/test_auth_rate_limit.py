"""Unit tests for the sliding-window login rate limiter."""

from __future__ import annotations

from server.auth.rate_limit import SlidingWindowLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_limiter(clock: FakeClock) -> SlidingWindowLimiter:
    return SlidingWindowLimiter(max_attempts=3, window_seconds=60, clock=clock)


def test_allows_up_to_max_attempts() -> None:
    limiter = make_limiter(FakeClock())
    for _ in range(3):
        assert limiter.allow("sid:A") is True
        limiter.record("sid:A")
    assert limiter.allow("sid:A") is False


def test_window_expiry_re_allows() -> None:
    clock = FakeClock()
    limiter = make_limiter(clock)
    for _ in range(3):
        limiter.record("sid:A")
    assert limiter.allow("sid:A") is False
    clock.now += 61
    assert limiter.allow("sid:A") is True


def test_reset_clears_attempts() -> None:
    limiter = make_limiter(FakeClock())
    for _ in range(3):
        limiter.record("sid:A")
    limiter.reset("sid:A")
    assert limiter.allow("sid:A") is True


def test_keys_are_independent() -> None:
    limiter = make_limiter(FakeClock())
    for _ in range(3):
        limiter.record("sid:A")
    assert limiter.allow("sid:A") is False
    assert limiter.allow("ip:1.2.3.4") is True


def test_partial_expiry_only_drops_old_attempts() -> None:
    clock = FakeClock()
    limiter = make_limiter(clock)
    limiter.record("k")
    limiter.record("k")
    clock.now += 30
    limiter.record("k")
    assert limiter.allow("k") is False
    clock.now += 31  # first two attempts now out of window, third remains
    assert limiter.allow("k") is True
