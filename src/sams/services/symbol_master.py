"""symbol_master 的周同步：整张 A/HK 清单拉回来，算好拼音两列再入库。

py_initials/py_full 在写时算（D141）：整词交给 pypinyin 才有多音字（银行的行读 háng），
但每次检索再算是把活放错了地方。降级链的「空」与「挂」分叉在 registry，这里只处理
一个决定：全链空答复 = 整表原样保留——一周一次的同步源同时挂掉与代码真没了，
对旧表是两种处置。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pypinyin import Style, lazy_pinyin

from sams.core.codes import currency_override
from sams.core.config import MarketCfg
from sams.datasource.registry import AdapterRegistry
from sams.storage.repos.symbol_list import SymbolListRepo

logger = logging.getLogger("sams.services.symbol_master")


def pinyin_columns(name: str) -> tuple[str, str]:
    cleaned = "".join(name.split())
    initials = "".join(lazy_pinyin(cleaned, style=Style.FIRST_LETTER)).lower()
    full = "".join(lazy_pinyin(cleaned, style=Style.NORMAL)).lower()
    return initials, full


@dataclass(frozen=True)
class SymbolMasterSummary:
    market: str
    source: str | None
    rows: int
    deactivated: int
    degraded: bool
    #: 全链空答复，旧表原样保留（D147）
    kept_old: bool = False
    #: 新名单比旧表在册数少一半，什么都不关停（D315）
    kept_floor: bool = False


class SymbolMasterSyncService:
    def __init__(
        self,
        registry: AdapterRegistry,
        symbol_list: SymbolListRepo,
        markets: dict[str, MarketCfg],
    ) -> None:
        self.registry = registry
        self.symbol_list = symbol_list
        self.markets = markets

    def sync(self, market: str) -> SymbolMasterSummary:
        result = self.registry.fetch_symbol_list(market)
        if result.empty_everywhere or result.frame.is_empty():
            logger.warning("%s 代码表所有源都答空，保留现有表", market)
            return SymbolMasterSummary(market, None, 0, 0, result.degraded, kept_old=True)
        # 在册数要在写入之前取：upsert 把新名单里的行全置成 is_active，写完再数就把
        # 「旧表在册多少只」与「这次写了多少只」混成同一个数
        before = self.symbol_list.count(market)
        rows = []
        for item in result.frame.to_dicts():
            name = (item["name"] or "").strip()
            initials, full = pinyin_columns(name) if name else ("", "")
            rows.append(
                {
                    "code": item["code"],
                    "name": name or None,
                    "py_initials": initials,
                    "py_full": full,
                    "currency": currency_override(market, item["code"])
                    or self.markets[market].currency,
                }
            )
        self.symbol_list.upsert_batch(market, rows)
        # 地板（D315）：D314 把「在册」变成了采集的前置条件，一次读小的名单不再只是搜索少
        # 几只——没进名单的票当晚起从每一条按只循环的链里掉出去，整片停更。真实退市是每周
        # 几十只的量级（实机三张快照一共关停 6 行），少一半这个地板拦不到它，拦的是源改版。
        if len(rows) * 2 < before:
            logger.warning(
                "%s 代码表只读到 %s 行，旧表在册 %s：比一半还少，不关停任何行",
                market,
                len(rows),
                before,
            )
            return SymbolMasterSummary(
                market, result.source, len(rows), 0, result.degraded, kept_floor=True
            )
        deactivated = self.symbol_list.deactivate_missing(market, [row["code"] for row in rows])
        logger.info(
            "%s 代码表 %s 行（源 %s），关停 %s", market, len(rows), result.source, deactivated
        )
        return SymbolMasterSummary(market, result.source, len(rows), deactivated, result.degraded)
