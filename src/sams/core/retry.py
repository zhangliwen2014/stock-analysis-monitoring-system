"""退避重试：sleep 可注入，因此「429 之后等多久」不需要真等被测。

这里刻意**不**定义 `Clock`：`datasource/ratelimit.py` 的那个 `Clock` 是单调秒表（给
TokenBucket 记账用），而 `core/clock.py::Clock` 是 `now_utc()`。两个同名不同义的合同
放进同一个包，读的人要在两个文件之间跳一次才知道手上那个 clock 是哪种。退避只需要 sleep。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

logger = logging.getLogger("sams.retry")

Sleeper = Callable[[float], None]


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
    retryable: tuple[type[BaseException], ...],
    label: str = "",
) -> object:
    """退避重试；最后一次仍失败则原样抛出，由调用方决定降级还是上抛。

    `retryable` 必填且没有默认值：默认值属于谁家的异常元组，这个模块就认识了谁家的上层。
    """
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
