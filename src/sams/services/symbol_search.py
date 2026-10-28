"""搜索加票的读模型：归一检索词 → 五档候选 → 贴上库内最近收盘。

最近收盘取本地 `bars_daily` 不取报价板：报价板上只有已订阅的票，而这一面板要能给
「还没加的票」报数（D142）。它可能停在几天前，所以列名不叫「昨收」。取不到就 None，
模板写「—」——空位是真话，0 是假话。
"""

from __future__ import annotations

from dataclasses import dataclass

from sams.core.codes import canonical_code, canonical_market
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.symbol_list import SymbolListRepo

#: 剥完至少剩两位才剥：`HK7` 那两位是代码本体的一部分，剥掉就查不到任何票
_PREFIXES = ("SH", "SZ", "BJ", "HK", "US")

#: 前缀背后的市场：主档的市场键是归一名，A 股的前缀却是三个交易所（D141）
_PREFIX_MARKETS = {"SH": "A", "SZ": "A", "BJ": "A", "HK": "HK", "US": "US"}


def hinted_market(raw: str) -> str | None:
    """检索词开头那个市场前缀指哪张市场：归一剥掉它就等于把用户的选择扔了。

    剥不动的前缀不算提示（与 `normalize_query` 同一条「至少剩两位」），
    否则 `HK7` 会把一个代码本体说成市场意图。
    """
    q = (raw or "").strip().upper().replace(" ", "")
    for prefix, market in _PREFIX_MARKETS.items():
        if q.startswith(prefix) and len(q) - len(prefix) >= 2:
            return market
    return None


def normalize_query(raw: str) -> str | None:
    q = (raw or "").strip().upper().replace(" ", "")
    for prefix in _PREFIXES:
        if q.startswith(prefix) and len(q) - len(prefix) >= 2:
            q = q[len(prefix) :]
            break
    q = q[:64]
    if q in _PREFIXES:
        # 光剩一个市场字母不是检索词：市场由表单下拉决定，这一串没有提供任何信息
        return None
    return q if q and any(ch.isalnum() for ch in q) else None


@dataclass(frozen=True)
class Candidate:
    market: str
    code: str
    name: str | None
    close: float | None


class SymbolSearchService:
    def __init__(self, symbol_list: SymbolListRepo, bars: BarRepo) -> None:
        self.symbol_list = symbol_list
        self.bars = bars

    def search(self, raw: str) -> list[Candidate]:
        q = normalize_query(raw)
        if q is None:
            return []
        hits = self.symbol_list.search(q)
        keys = [
            (canonical_market(h["market"]), canonical_code(h["market"], h["code"])) for h in hits
        ]
        closes = self.bars.last_closes(keys)
        return [
            Candidate(market, code, h["name"], closes.get((market, code)))
            for h, (market, code) in zip(hits, keys, strict=True)
        ]
