"""on_poll 这道缝：求值挂在 poll_once 之后。坏了只许记日志，不许拖垮轮询。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from support import FrozenClock

from sams.core.errors import SamsError
from sams.core.quotes import Quote
from sams.services.intraday import IntradayMonitor
from sams.services.quotes import QuotePollResult

NOW = datetime(2026, 10, 23, 3, 0, tzinfo=UTC)
QUOTE = Quote(market="A", code="601088", ts=NOW, price=9.4, source="tencent")


class _Board:
    def __init__(self) -> None:
        self.skipped: list[tuple[str, str]] = []

    def skip(self, market, reason, *, now):
        self.skipped.append((market, reason))

    def fail(self, market, error, *, now):
        self.failed = (market, error)


class _Quotes:
    def __init__(self) -> None:
        self.board = _Board()

    def poll(self, market, codes, *, now=None):
        return QuotePollResult(quotes=(QUOTE,), skipped=(), errors=(), sources=())


class _Calendar:
    def is_trading_day(self, market, day):
        return True

    def is_market_open(self, market, moment):
        return True


class _Universe:
    def build(self):
        # poll_once 先问 build() 拿到「此刻的宇宙」，再逐市场问 codes
        return self

    def codes(self, market):
        return ("601088",)

    def indices(self, market):
        return ()


def monitor(settings, on_poll) -> IntradayMonitor:
    # settings夹具带 A/HK/US 三个市场；本模块只关心「一轮=一个市场」的挂钩语义，
    # 收窄到单市场，len(got) == 1 的断言才有意义（HK/US 会被日历/时段路径干扰）
    single = replace(settings, markets={"A": settings.markets["A"]})
    return IntradayMonitor(
        single, _Quotes(), _Calendar(), _Universe(),
        clock=FrozenClock(NOW), on_poll=on_poll,
    )


def test_poll_once_calls_on_poll_with_the_round(settings):
    seen: list = []
    got = monitor(settings, seen.append).poll_once(now=NOW)
    assert len(seen) == 1
    assert seen[0] == got and len(got) == 1 and got[0].market == "A"


def test_a_sams_error_from_the_hook_does_not_break_the_poll(settings):
    def broken(outcomes):
        raise SamsError("评估这一轮做不成")

    got = monitor(settings, broken).poll_once(now=NOW)  # 不抛
    assert len(got) == 1


def test_a_programming_error_still_propagates(settings):
    def broken(outcomes):
        raise RuntimeError("真 bug")

    with pytest.raises(RuntimeError):
        monitor(settings, broken).poll_once(now=NOW)


def test_without_a_hook_nothing_happens(settings):
    got = monitor(settings, None).poll_once(now=NOW)
    assert len(got) == 1
