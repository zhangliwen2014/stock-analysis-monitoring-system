"""搜索加票的读模型：归一检索词 → 五档候选 → 贴上库内昨收。

昨收取本地 `bars_daily` 不取报价板：报价板上只有已订阅的票，而这一面板要能给
「还没加的票」报数（D142）。取不到就 None，模板写「—」——空位是真话，0 是假话。
"""

from __future__ import annotations

from dataclasses import dataclass

from sams.core.codes import canonical_code, canonical_market
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.symbol_list import SymbolListRepo

#: 剥前缀要求剩下至少两位：`HK7` 那两位是代码本体的一部分，剥掉就查不到任何票
_PREFIXES = ("SH", "SZ", "BJ", "HK", "US")


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
