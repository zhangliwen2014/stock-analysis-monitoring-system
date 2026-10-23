from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from sams.datasource.ratelimit import TokenBucket


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


def test_reserve_does_not_lose_tokens_under_threads():
    """盘中轮询与盘后同步共用同一个桶：扣减必须串行。

    没有锁时，两边可能读到同一个 `_level`、各自扣一次、后写的覆盖先写的——
    症状不是报错，而是限速被悄悄放宽（或收紧）了一倍。
    """
    start = threading.Barrier(8)

    def slow_clock():
        time.sleep(0.002)
        return 0.0

    bucket = TokenBucket(rate=1.0, capacity=1.0, clock=slow_clock)

    def take(_index):
        start.wait()
        return bucket.reserve(1.0)

    original = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            waits = list(pool.map(take, range(8)))
    finally:
        sys.setswitchinterval(original)

    assert sorted(waits) == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]
