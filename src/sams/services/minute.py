"""分钟粒度：分时与分钟 K 的读取面（D163/D164/D165）。

三条职责，都只读、都不落库：

  1. `availability`——这一市场的两档谁能答、不能答时怎么说。周期条上的置灰与
     接口返回的 `note` 共用这一份，屏上和 JSON 里不会各说一套（D165）。
  2. `timeshare`——把腾讯给的**累计**量差分回每分钟，并算出均价线。
  3. `bars`——把分钟 K 组装成 `AdjustedSeries` 交给 `analysis.metrics`（D164）。

缓存的是**取回来的帧**而不是算好的表：换一组指标勾选不该再打一次外网，
而把 `metrics` 的结果缓存下来会让「同一份数两种指标」在缓存里并存。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.datasource.capabilities import capability
from sams.datasource.minute import answers

#: 屏幕上那六档分钟 K：键 = URL 里那个 `period`，值 = 适配器要的 `scale`（分钟数）
MINUTE_SCALES: dict[str, int] = {
    "m1": 1,
    "m5": 5,
    "m15": 15,
    "m30": 30,
    "m60": 60,
    "m120": 120,
}


@dataclass(frozen=True)
class KindAnswer:
    """一档（分时 / 分钟 K）在某个市场的可用性。"""

    label: str
    ok: bool
    #: 链上有这个方法但关掉了这一市场的源：`(源名, 原因)`
    refused: tuple[tuple[str, str], ...] = ()

    @property
    def why_not(self) -> str:
        """不能答时的那句话。

        两种「不能」必须分开：链上根本没有这个方法的源（配置里就没接），
        与有方法但这个市场不答（接了，腾讯不吃美股的分时）——后者要念出源名，
        否则读的人以为是自己没配。
        """
        if self.refused:
            names = "、".join(name for name, _reason in self.refused)
            return f"{names} 不供这一市场的{self.label}"
        return f"这条链上没有能提供{self.label}的源"


@dataclass(frozen=True)
class MinuteAvailability:
    market: str
    series: KindAnswer
    bars: KindAnswer


class MinuteService:
    def __init__(
        self,
        registry,
        calendar,
        *,
        clock: Clock = SYSTEM_CLOCK,
        ttl_s: float = 60.0,
        now=time.monotonic,
    ) -> None:
        self._registry = registry
        self._calendar = calendar
        self._clock = clock
        self._ttl = ttl_s
        self._now = now
        #: `(market, code, period)` → `(装填时刻, 帧)`：见模块 docstring
        self._cache: dict[tuple[str, str, str], tuple[float, object]] = {}

    def availability(self, market: str) -> MinuteAvailability:
        return MinuteAvailability(
            market,
            self._answer(market, "minute_series"),
            self._answer(market, "minute_bars"),
        )

    def _answer(self, market: str, method: str) -> KindAnswer:
        """`chain_for`（有没有这个方法）× `answers`（答不答这个市场）——两问都得问。

        只查前者会把「腾讯有 minute_bars 但美股不答」算成可用：置灰失效，
        点下去收到的是一句接口错误而不是「这一档不供美股」。
        """
        cap = capability(method)
        chain = self._registry.chain_for(market, method)
        willing: list[str] = []
        refused: list[tuple[str, str]] = []
        for name in chain:
            adapter = self._registry.adapter(name)
            if answers(adapter, method, market):
                willing.append(name)
            else:
                refused.append((name, f"{cap.label}不答 {market}"))
        return KindAnswer(cap.label, bool(willing), tuple(refused))
