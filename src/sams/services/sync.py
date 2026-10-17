"""日线同步用例：降级取数 → 补主档 → 幂等入库。

分工：「用哪个源、失败了换谁」在 datasource.registry，「存成什么」在 storage.repos，
这里只回答「一次同步需要几步、结果如何汇报」。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

import polars as pl

from sams.core.config import MarketCfg
from sams.core.errors import DatasourceError
from sams.datasource.registry import AdapterRegistry, Attempt
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.symbols import SymbolRepo

logger = logging.getLogger("sams.services.sync")


@dataclass(frozen=True)
class SyncSummary:
    market: str
    code: str
    source: str = ""
    fetched: int = 0
    written: int = 0
    degraded: bool = False
    #: 所有源都说这个区间没有数据（停牌、新股、区间过短）——不是失败
    empty: bool = False
    latest_date: date | None = None
    attempts: tuple[Attempt, ...] = ()
    error: str | None = None


class SyncService:
    def __init__(
        self,
        registry: AdapterRegistry,
        bars: BarRepo,
        symbols: SymbolRepo,
        markets: dict[str, MarketCfg],
    ) -> None:
        self.registry = registry
        self.bars = bars
        self.symbols = symbols
        self.markets = markets

    def currency_of(self, market: str) -> str:
        try:
            return self.markets[market].currency
        except KeyError:
            raise DatasourceError("sync", f"市场 {market} 未配置币种") from None

    def sync_daily(self, market: str, code: str, start: date, end: date) -> SyncSummary:
        """幂等：同区间重复执行行数不变（bars_daily 主键 + INSERT OR REPLACE）。"""
        result = self.registry.fetch_daily(market, code, start, end)
        if result.empty_everywhere:
            logger.info("%s %s 区间内所有源均无数据", market, code)
            return SyncSummary(
                market=market,
                code=code,
                empty=True,
                latest_date=self.bars.latest_date(market, code),
                attempts=result.attempts,
            )

        self._ensure_symbol(market, code)
        written = self.bars.upsert(result.frame)
        return SyncSummary(
            market=market,
            code=code,
            source=result.source,
            fetched=result.frame.height,
            written=written,
            degraded=result.degraded,
            latest_date=self.bars.latest_date(market, code),
            attempts=result.attempts,
        )

    def _ensure_symbol(self, market: str, code: str) -> None:
        """只在主档缺失时补一行。

        已有行可能带名称/交易所/板块，采集不该每次都把它们抹成 null。
        """
        if self.symbols.get(market, code) is not None:
            return
        self.symbols.upsert(
            pl.DataFrame(
                [{"market": market, "code": code, "currency": self.currency_of(market)}]
            )
        )
