"""盘中轮询的节拍器：什么时候问、问哪些、跳过时留下什么（spec §4.3）。

只做三件事：

1. 每个市场用交易日历判断「现在该不该问」——非交易日、午休、收盘后一律跳过，
   并把原因写到板上（页面要能解释「为什么没有新报价」）；
2. 构建这一轮的对象全集（自选 ∪ 持仓 ∪ 规则目标 ∪ 在跟观点）；
3. 调 `QuoteService.poll`。取价、降级、落库、告警都在那一层。

**不在这里评估规则**：那一层吃 `QuotePollResult`，由装配决定它跟不跟在这个循环后面——
这道缝落实为 `IntradayMonitor` 的 `on_poll` 构造参数，本层依旧不认识规则。
也不 import 调度器：`UniverseBuilder` 只需要一个 `build()` → `.codes()/.indices()`
的形状，把整个调度器拖进来会让「盘中」依赖「盘后」的内部。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.config import Settings
from sams.core.errors import SamsError
from sams.core.retry import Sleeper
from sams.services.quotes import QuotePollResult, QuoteService

logger = logging.getLogger("sams.services.intraday")


class UniverseView(Protocol):
    def codes(self, market: str) -> tuple[str, ...]: ...

    def indices(self, market: str) -> tuple[str, ...]: ...


class UniverseSource(Protocol):
    def build(self) -> UniverseView: ...


@dataclass(frozen=True)
class MarketPoll:
    market: str
    #: 这一市场被跳过的原因；被跳过时 `result` 为空
    skipped: str | None = None
    #: 这一市场取价整段失败（与「源返回 0 条」不同）
    error: str | None = None
    result: QuotePollResult | None = None


class IntradayMonitor:
    def __init__(
        self,
        settings: Settings,
        quotes: QuoteService,
        calendar,
        universe: UniverseSource,
        *,
        clock: Clock = SYSTEM_CLOCK,
        sleep: Sleeper = time.sleep,
        interval_s: float | None = None,
        on_poll: Callable[[tuple[MarketPoll, ...]], object] | None = None,
    ) -> None:
        self.settings = settings
        self.quotes = quotes
        self.calendar = calendar
        self.universe = universe
        self.clock = clock
        self.sleep = sleep
        self.interval_s = interval_s if interval_s is not None else settings.polling_interval_s
        # 这一层不认识规则——挂钩是装配注进来的；它坏了不许拖垮取价。
        self.on_poll = on_poll

    def poll_once(self, *, now: datetime | None = None) -> tuple[MarketPoll, ...]:
        moment = now or self.clock.now_utc()
        universe = self.universe.build()
        outcomes: list[MarketPoll] = []
        for market in sorted(self.settings.markets):
            reason = self._skip_reason(market, moment)
            if reason is not None:
                self.quotes.board.skip(market, reason, now=moment)
                outcomes.append(MarketPoll(market, skipped=reason))
                continue
            # 票跟着用户走，指数跟着设置走：自选全空的用户也要看到上证在跳（D145）
            codes = list(universe.codes(market)) + list(universe.indices(market))
            if not codes:
                # 开市中但没有任何对象：不是故障，但页面要能说清「为什么没动」
                self.quotes.board.skip(market, "没有需要监控的标的", now=moment)
                outcomes.append(MarketPoll(market, skipped="没有需要监控的标的"))
                continue
            try:
                result = self.quotes.poll(market, codes, now=moment)
            except SamsError as exc:
                # 只隔离这一个市场：日历源坏了不该让排在后面的市场也不跑
                logger.exception("%s 这一轮取价失败", market)
                self.quotes.board.fail(market, str(exc), now=moment)
                outcomes.append(MarketPoll(market, error=str(exc)))
                continue
            outcomes.append(MarketPoll(market, result=result))
        if self.on_poll is not None:
            try:
                self.on_poll(tuple(outcomes))
            except SamsError:
                # 求值失败不回收这轮行情：板子上的价照常更新，下一轮还会再判
                logger.exception("盘中规则评估这一轮失败")
        return tuple(outcomes)

    def run_forever(
        self,
        *,
        stop: threading.Event | None = None,
        on_round=None,
        max_rounds: int | None = None,
    ) -> int:
        """一轮接一轮地跑，返回实际跑过的轮数。

        只接 `SamsError`：那是「这一轮做不成」（账本读不到、日历源坏了），循环本身
        没坏。编程错误照旧炸出去——接住它之后每一轮都失败却还在打日志，比停下更糟。
        """
        rounds = 0
        while max_rounds is None or rounds < max_rounds:
            if stop is not None and stop.is_set():
                break
            try:
                outcomes = self.poll_once()
            except SamsError as exc:
                logger.exception("盘中这一轮失败：%s", exc)
                outcomes = ()
            if on_round is not None:
                on_round(outcomes)
            rounds += 1
            if max_rounds is not None and rounds >= max_rounds:
                break
            if stop is not None:
                if stop.wait(self.interval_s):
                    break
            else:
                self.sleep(self.interval_s)
        return rounds

    def _skip_reason(self, market: str, moment: datetime) -> str | None:
        """常规时段才问价（D114）：盘前盘后不轮询，量会凭空放大且报价不代表常规成交。"""
        cfg = self.settings.markets[market]
        local_day = moment.astimezone(cfg.zone()).date()
        if not self.calendar.is_trading_day(market, local_day):
            return "非交易日"
        if not self.calendar.is_market_open(market, moment):
            return "非交易时段（未开盘/午休/已收盘）"
        return None
