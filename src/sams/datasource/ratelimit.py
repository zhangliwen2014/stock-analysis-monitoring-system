"""限速与退避。

时钟与 sleep 注入：限速逻辑必须能在假时钟下被确定性地测试，
否则「429 之后退避多久」这种关键行为只能靠真等。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from sams.core.errors import RateLimited

logger = logging.getLogger("sams.datasource.ratelimit")

Clock = Callable[[], float]
Sleeper = Callable[[float], None]

RETRYABLE = (RateLimited, OSError, TimeoutError)


class TokenBucket:
    """漏桶式预约令牌：返回「需要等多久」，预约即刻扣减，因此并发请求会排队。"""

    def __init__(
        self,
        rate: float,
        capacity: float | None = None,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleeper = time.sleep,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate 必须为正")
        self.rate = rate
        self.capacity = capacity if capacity is not None else max(rate, 1.0)
        self.clock = clock
        self.sleep = sleep
        self._level = float(self.capacity)
        self._updated = self.clock()

    def _refill(self) -> None:
        now = self.clock()
        self._level = min(self.capacity, self._level + (now - self._updated) * self.rate)
        self._updated = now

    def reserve(self, tokens: float = 1.0) -> float:
        """扣减 tokens，返回调用方应等待的秒数（0 表示可立即执行）。"""
        self._refill()
        self._level -= tokens
        return 0.0 if self._level >= 0 else -self._level / self.rate

    def acquire(self, tokens: float = 1.0) -> float:
        wait = self.reserve(tokens)
        if wait > 0:
            self.sleep(wait)
        return wait


def backoff_delays(attempts: int, *, base: float = 1.0, factor: float = 4.0) -> list[float]:
    """重试之间的等待序列：3 次尝试 → [1, 4]（第 3 次失败后不再等）。"""
    if attempts < 1:
        raise ValueError("attempts 至少为 1")
    return [base * factor**i for i in range(attempts - 1)]


def call_with_backoff(
    func: Callable[[], object],
    *,
    attempts: int = 3,
    base: float = 1.0,
    factor: float = 4.0,
    sleep: Sleeper = time.sleep,
    retryable: tuple[type[BaseException], ...] = RETRYABLE,
    label: str = "",
) -> object:
    """退避重试；最后一次仍失败则原样抛出，由调用方（路由器）决定降级还是上抛。"""
    delays = backoff_delays(attempts, base=base, factor=factor)
    last: BaseException | None = None
    for index in range(attempts):
        try:
            return func()
        except retryable as exc:
            last = exc
            if index < len(delays):
                logger.warning(
                    "%s 第 %d 次失败（%s），%.1fs 后重试",
                    label or "调用",
                    index + 1,
                    type(exc).__name__,
                    delays[index],
                )
                sleep(delays[index])
    assert last is not None  # 循环必然至少失败一次才会走到这里
    raise last
