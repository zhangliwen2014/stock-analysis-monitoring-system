"""「有人想看这只票」的登记处：主档建行 + 补数排队，一次调用两件事。

四个入口（自选/成交/论点/规则）都汇到这里，市场与代码在这里归一——入口各归一遍
迟早有一处忘，`/symbol/HK/700` 那种「页面在、数据不在」就是这么来的。
actor 只进日志：谁加的票是排查线索，不是数据（D144）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import polars as pl

from sams.core.codes import canonical_code, canonical_market, currency_override, lot_size_of
from sams.core.config import MarketCfg

logger = logging.getLogger("sams.services.symbol_interest")


@dataclass(frozen=True)
class EnsureReport:
    market: str
    code: str
    created_master: bool
    queued: bool


class SymbolInterest:
    def __init__(
        self, *, symbols, bars, symbol_list, backfills, markets: dict[str, MarketCfg]
    ) -> None:
        self.symbols = symbols
        self.bars = bars
        self.symbol_list = symbol_list
        self.backfills = backfills
        self.markets = markets

    def ensure(self, market: str, code: str, *, actor: str) -> EnsureReport:
        market = canonical_market(market)
        code = canonical_code(market, code)
        created = False
        if self.symbols.get(market, code) is None:
            listed = self.symbol_list.get(market, code) or {}
            self.symbols.upsert(
                pl.DataFrame(
                    [
                        {
                            "market": market,
                            "code": code,
                            "name": listed.get("name"),
                            "currency": listed.get("currency")
                            or currency_override(market, code)
                            or self.markets[market].currency,
                            "lot_size": lot_size_of(market),
                        }
                    ]
                )
            )
            created = True
        queued = False
        if self.bars.count(market, code) == 0:
            queued = self.backfills.enqueue(market, code)
        logger.info(
            "%s %s 登记兴趣 actor=%s 建主档=%s 排队=%s", market, code, actor, created, queued
        )
        return EnsureReport(market, code, created, queued)
