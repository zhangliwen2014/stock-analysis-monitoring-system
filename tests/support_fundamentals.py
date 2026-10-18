"""CLI 端的基本面/估值替身：一个长出三种能力的源。

`support.StubAdapter` 只有日线（+可选公司行为），而服务层的降级链按**方法是否存在**
路由（D38）：给测试塞一个只有日线的桩，基本面链会把它整个跳过，命令看起来跑通了、
其实一行数据都没问过。
"""

from __future__ import annotations

from datetime import date

import polars as pl

from sams.datasource.fundamentals import empty_financials, empty_flow, empty_valuation


class StubFundamentalsAdapter:
    name = "stubfin"

    def __init__(
        self,
        markets=("A",),
        *,
        financials=None,
        valuation=None,
        flow=None,
        fail: Exception | None = None,
    ) -> None:
        self.markets = frozenset(markets)
        self._financials = financials if financials is not None else empty_financials()
        self._valuation = valuation if valuation is not None else empty_valuation()
        self._flow = flow if flow is not None else empty_flow()
        self._fail = fail
        self.calls: list[str] = []

    def financials(self, market: str, code: str, start: date, end: date) -> pl.DataFrame:
        return self._answer("financials", self._financials, market, code)

    def valuation(self, market: str, code: str, start: date, end: date) -> pl.DataFrame:
        return self._answer("valuation", self._valuation, market, code)

    def funding_flow(self, market: str, code: str, start: date, end: date) -> pl.DataFrame:
        return self._answer("funding_flow", self._flow, market, code)

    def _answer(self, method: str, frame: pl.DataFrame, market: str, code: str) -> pl.DataFrame:
        self.calls.append(method)
        if self._fail is not None:
            raise self._fail
        # 只回这只票的行：桩与真源一样，不能让 A 的数据被读到 B 名下
        return frame.filter((pl.col("market") == market) & (pl.col("code") == code))
