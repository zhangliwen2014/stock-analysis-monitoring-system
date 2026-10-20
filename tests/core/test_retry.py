"""退避：全项目唯一一处「重试几次、间隔多久」。

住在 core 的理由：采集层（换源前的重试）与通知层（发送失败的重试）问的是同一个问题。
留在 datasource 里，通知层要复用就得 import datasource——而 `monitor` 的许可是
`{core, storage}`，那条 import 会被守卫挡下。真正的 alternative 是抄一份，
抄的那一份会在某一次只改一边之后开始给出不同的「还要等多久」。
"""

from __future__ import annotations

import pytest

from sams.core.errors import RateLimited, TransientError
from sams.core.retry import backoff_delays, call_with_backoff
from sams.datasource.ratelimit import RETRYABLE


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

    # retryable 是必填：core 里不许藏一个属于上层的默认元组
    result = call_with_backoff(flaky, sleep=waits.append, retryable=RETRYABLE, label="test")
    assert result == "ok"
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
        call_with_backoff(always, attempts=2, sleep=waits.append, retryable=RETRYABLE)
    assert waits == [1.0]


def test_retryable_is_a_required_keyword():
    """必填要被机器钉住：给了默认值，「忘了传」与「故意用默认」在代码里长得一样。"""
    with pytest.raises(TypeError):
        call_with_backoff(lambda: None)


def test_transient_error_is_retryable_by_datasource_chain():
    """上面那条 `RETRYABLE` 的引用是给 datasource 用的；core 不认识它，所以这里只确认
    上提没有把异常归类改掉：TransientError 不在 RETRYABLE 里，路由器要按 D88 自己判。"""
    assert TransientError not in RETRYABLE
