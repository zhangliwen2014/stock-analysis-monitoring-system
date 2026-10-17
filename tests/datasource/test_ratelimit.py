from __future__ import annotations

import pytest

from sams.core.errors import RateLimited
from sams.datasource.ratelimit import TokenBucket, backoff_delays, call_with_backoff


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_bucket(rate: float, capacity: float | None = None):
    clock = FakeClock()
    waits: list[float] = []
    bucket = TokenBucket(rate, capacity, clock=clock, sleep=waits.append)
    return bucket, clock, waits


def test_bucket_rate_must_be_positive():
    with pytest.raises(ValueError, match="rate"):
        TokenBucket(0)


def test_burst_is_free_then_paced():
    bucket, clock, waits = make_bucket(rate=2.0, capacity=2.0)
    assert bucket.acquire() == 0.0
    assert bucket.acquire() == 0.0
    assert bucket.acquire() == 0.5, "3 次请求 / 2 容量 / 2 每秒 → 第 3 次要等 0.5s"
    assert waits == [0.5], "等待必须走注入的 sleep，不能真等"

    # 欠下 2 个令牌（4 次请求 − 2 容量），要等满 1 秒才清空，光等 0.5 秒还得再等
    clock.advance(1.0)
    assert bucket.acquire() == 0.0


def test_reserve_does_not_sleep_but_queues_concurrent_callers():
    """预约即扣减：后来的调用方拿到更长的等待时间，而不是都以为自己插到了队首。"""
    bucket, _, waits = make_bucket(rate=1.0, capacity=1.0)
    assert bucket.reserve() == 0.0
    assert bucket.reserve() == 1.0
    assert bucket.reserve() == 2.0
    assert waits == []


def test_refill_is_capped_at_capacity():
    bucket, clock, _ = make_bucket(rate=1.0, capacity=3.0)
    bucket.reserve(3.0)
    clock.advance(1000)  # 长时间空闲也不能攒出无限额度
    assert bucket.reserve(3.0) == 0.0
    assert bucket.reserve(1.0) == 1.0


def test_backoff_sequence_is_exponential_by_four():
    assert backoff_delays(1) == []
    assert backoff_delays(3) == [1.0, 4.0]
    assert backoff_delays(4, base=2.0) == [2.0, 8.0, 32.0]
    with pytest.raises(ValueError):
        backoff_delays(0)


def test_call_with_backoff_retries_then_succeeds():
    waits: list[float] = []
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RateLimited("eastmoney", "429")
        return "ok"

    assert call_with_backoff(flaky, sleep=waits.append, label="test") == "ok"
    assert calls["n"] == 3
    assert waits == [1.0, 4.0]


def test_non_retryable_error_fails_immediately():
    waits: list[float] = []
    calls = {"n": 0}

    def broken():
        calls["n"] += 1
        raise ValueError("结构不对")

    with pytest.raises(ValueError):
        call_with_backoff(broken, sleep=waits.append, retryable=(RateLimited,))
    assert calls["n"] == 1 and waits == []


def test_last_error_is_raised_after_exhausting_attempts():
    waits: list[float] = []

    def always():
        raise OSError("网络断了")

    with pytest.raises(OSError, match="网络断了"):
        call_with_backoff(always, attempts=2, sleep=waits.append)
    assert waits == [1.0]
