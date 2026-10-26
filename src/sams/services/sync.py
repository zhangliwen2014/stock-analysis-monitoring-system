"""日线同步用例：降级取数 → 补主档 → 幂等入库。

指数日线（`sync_index`）复用同一条链，但既不补主档也不归一代码——它进的是另一张表。
分工：「用哪个源、失败了换谁」在 datasource.registry，「存成什么」在 storage.repos，
这里只回答「一次同步需要几步、结果如何汇报」。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

import polars as pl

from sams.core.codes import canonical_code, canonical_market, currency_override, lot_size_of
from sams.core.config import MarketCfg
from sams.core.errors import DatasourceError
from sams.datasource.registry import AdapterRegistry, Attempt
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.index_daily import IndexDailyRepo
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
        *,
        indices: IndexDailyRepo | None = None,
    ) -> None:
        self.registry = registry
        self.bars = bars
        self.symbols = symbols
        self.markets = markets
        #: 指数日线仓库：只有大盘条那条链用得到，所以它不跟着 `bars` 必填
        self.indices = indices

    def currency_of(self, market: str, code: str) -> str:
        try:
            default = self.markets[market].currency
        except KeyError:
            raise DatasourceError("sync", f"市场 {market} 未配置币种") from None
        return currency_override(market, code) or default

    def sync_daily(self, market: str, code: str, start: date, end: date) -> SyncSummary:
        """幂等：同区间重复执行行数不变（bars_daily 主键 + INSERT OR REPLACE）。

        代码在入口就归一：主档、K 线、读取回看用同一个形态，否则别名会把同一只票
        存成多份主键。市场名同理——`--market us` 会在主档里造出一个 `us` 的行，
        而 K 线仍然按源给的 `US` 入库，同一只票从此有两个键。
        """
        market = canonical_market(market)
        code = canonical_code(market, code)
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

    def sync_index(self, market: str, index_code: str, start: date, end: date) -> SyncSummary:
        """指数日线：降级链与个股同一条，但不补主档——指数不是票。

        帧里那列 `code` 换成 `index_code` 再入库：指数表与个股表分键（000001 两边都占）。
        """
        assert self.indices is not None  # job 登记前已查过能力与注入，这里只兜编程错误
        result = self.registry.fetch_index_daily(market, index_code, start, end)
        if result.empty_everywhere:
            return SyncSummary(
                market=market,
                code=index_code,
                empty=True,
                latest_date=self.indices.latest_date(market, index_code),
                attempts=result.attempts,
            )
        frame = result.frame.rename({"code": "index_code"})
        written = self.indices.upsert(frame)
        return SyncSummary(
            market=market,
            code=index_code,
            source=result.source,
            fetched=frame.height,
            written=written,
            degraded=result.degraded,
            latest_date=self.indices.latest_date(market, index_code),
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
                [
                    {
                        "market": market,
                        "code": code,
                        "currency": self.currency_of(market, code),
                        "lot_size": lot_size_of(market),
                    }
                ]
            )
        )
