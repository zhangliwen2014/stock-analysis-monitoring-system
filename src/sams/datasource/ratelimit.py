"""限速与退避。

时钟与 sleep 注入：限速逻辑必须能在假时钟下被确定性地测试，
否则「429 之后退避多久」这种关键行为只能靠真等。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from sams.core.errors import RateLimited
from sams.core.retry import Sleeper, backoff_delays, call_with_backoff

logger = logging.getLogger("sams.datasource.ratelimit")

#: 单调秒表：只给 TokenBucket 记账用，与 `core.clock.Clock`（now_utc）不是一回事。
Clock = Callable[[], float]

RETRYABLE = (RateLimited, OSError, TimeoutError)

#: `backoff_delays`/`call_with_backoff` 的定义已上提 `core.retry`；这里重新导出是为了
#: 让「按源退避」和「按源限速」仍然从一个模块拿——消费者不该知道内部搬过家。
__all__ = [
    "Clock",
    "RETRYABLE",
    "Sleeper",
    "TokenBucket",
    "backoff_delays",
    "call_with_backoff",
]


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
        #: 盘中轮询与盘后同步共用一个桶：没有锁时两边各自 `_refill` 再扣减，
        #: 后写的 `_level` 会覆盖先写的，限速被悄悄放宽
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = self.clock()
        self._level = min(self.capacity, self._level + (now - self._updated) * self.rate)
        self._updated = now

    def reserve(self, tokens: float = 1.0) -> float:
        """扣减 tokens，返回调用方应等待的秒数（0 表示可立即执行）。

        `sleep` 留在锁外（见 `acquire`）：持锁等待会把所有调用方串行到一次 HTTP 的
        时长上，那不是限速，是排队超时。
        """
        with self._lock:
            self._refill()
            self._level -= tokens
            return 0.0 if self._level >= 0 else -self._level / self.rate

    def acquire(self, tokens: float = 1.0) -> float:
        wait = self.reserve(tokens)
        if wait > 0:
            self.sleep(wait)
        return wait
