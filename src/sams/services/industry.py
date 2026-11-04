"""行业回填：把公司概况里的行业落到主档 `sector`，只做归一，不建表、不联网（D236）。

它排在数据链（含 `profile`）之后、`spot` 之前：读的是今晚刚同步完的概况。
三条分支各有各的计数，因为它们下一步的处置不同：

* 概况有行业 → 写主档（`changed` / `unchanged`）；
* 概况没行业 → **保留旧值**（`kept_stale`），清空会让持仓页的行业分组消失；
* 主档没有这一行 → 只计数（`missing_symbol`），主档只由代码表建。
"""

from __future__ import annotations

from dataclasses import dataclass

from sams.core.codes import canonical_market
from sams.core.industry import normalize_industry
from sams.storage.repos.f10 import CompanyProfileRepo
from sams.storage.repos.symbols import SymbolRepo


@dataclass(frozen=True)
class IndustryBackfill:
    market: str
    scanned: int = 0
    changed: int = 0
    unchanged: int = 0
    missing_industry: int = 0
    kept_stale: int = 0
    missing_symbol: int = 0

    @property
    def note(self) -> str:
        return (
            f"概况 {self.scanned} 只，回填 {self.changed} 只，未变 {self.unchanged} 只，"
            f"缺行业 {self.missing_industry} 只，主档缺行 {self.missing_symbol} 只"
            + (f"，保留旧值 {self.kept_stale} 只" if self.kept_stale else "")
        )


class IndustryService:
    def __init__(self, profiles: CompanyProfileRepo, symbols: SymbolRepo) -> None:
        self.profiles = profiles
        self.symbols = symbols

    def industry_of(self, market: str, code: str) -> str:
        """一只票的行业：主档 `sector` → 概况 `industry`/`industry_csrc` → 空（归「未分类」）。

        两者都先过 `normalize_industry`：回填写的是归一后的路径，旧行可能是全角/斜杠，
        同一只票不能因为来源旧一点就在持仓页多出一个行业。拿不到就说没有，
        不按市场猜一个。
        """
        found = self.symbols.get(market, code) or {}
        sector = normalize_industry(found.get("sector"))
        if sector:
            return sector
        profile = self.profiles.get(market, code)
        if profile.is_empty():
            return ""
        row = profile.to_dicts()[0]
        return normalize_industry(row.get("industry") or row.get("industry_csrc"))

    def backfill(self, market: str) -> IndustryBackfill:
        market = canonical_market(market)
        # 主档一次读全：按只查会在 500 只的市场上多出 500 次往返，而这里只需要 code→sector
        known = {
            str(row["code"]): row
            for row in self.symbols.list_market(market, listed_only=False).to_dicts()
        }
        changed = unchanged = missing_industry = kept_stale = missing_symbol = 0
        rows = self.profiles.list_market(market).to_dicts()
        for row in rows:
            code = str(row["code"])
            symbol = known.get(code)
            if symbol is None:
                # 概况里有、主档里没有：不替代码表建行，只让调用方看见这个缺口
                missing_symbol += 1
                continue
            normalized = normalize_industry(row.get("industry"))
            if not normalized:
                if (symbol.get("sector") or "").strip():
                    kept_stale += 1
                else:
                    missing_industry += 1
                continue
            if self.symbols.set_sector(market, code, normalized):
                changed += 1
            else:
                unchanged += 1
        return IndustryBackfill(
            market=market,
            scanned=len(rows),
            changed=changed,
            unchanged=unchanged,
            missing_industry=missing_industry,
            kept_stale=kept_stale,
            missing_symbol=missing_symbol,
        )
