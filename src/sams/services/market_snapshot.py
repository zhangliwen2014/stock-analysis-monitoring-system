"""全市场快照：票池 → 取数 → 定日 → 整日落库 → 把源答不出的票点名。

票池来自 `symbol_master` 的活跃代码（D187）：不用 `financials.synced_codes`，那是
「同步过财务的票」，与一个市场的几千只差三个数量级——用它当票池，屏幕上那句范围
就在替人做一次没做过的普查。

日从**这一批报价时刻的众数**来，不从墙钟来：节假日补采时今天没有报价，落在今天
等于把上周五的价格写成今天的快照。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import polars as pl

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.codes import canonical_code, canonical_market
from sams.core.errors import ValidationError
from sams.storage.repos.snapshots import SnapshotRepo
from sams.storage.repos.symbol_list import SymbolListRepo


@dataclass(frozen=True)
class SnapshotSync:
    market: str
    day: date
    taken_at: datetime
    rows: int
    universe_size: int
    #: 请求了、源没有回的代码（不是「没有数」，是「这次没问到」——两者下一步不同）
    skipped: tuple[str, ...] = ()
    source: str = ""
    empty_everywhere: bool = False


class MarketSnapshotService:
    def __init__(
        self,
        registry,
        symbols: SymbolListRepo,
        snapshots: SnapshotRepo,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.registry = registry
        self.symbols = symbols
        self.snapshots = snapshots
        self.clock = clock

    def sync(
        self, market: str, *, codes: list[str] | None = None, day: date | None = None
    ) -> SnapshotSync:
        market = canonical_market(market)
        wanted = (
            [canonical_code(market, code) for code in codes]
            if codes is not None
            else self.symbols.active_codes(market)
        )
        if not wanted:
            raise ValidationError(
                f"{market} 的代码表里没有在市代码：先跑 "
                f"`sams run once --job symbol_master --market {market}`"
            )
        result = self.registry.fetch_market_snapshot(market, wanted)
        taken_at = self.clock.now_utc()
        if result.empty_everywhere:
            return SnapshotSync(
                market=market,
                day=day or self.clock.today(),
                taken_at=taken_at,
                rows=0,
                universe_size=len(wanted),
                source=result.source,
                empty_everywhere=True,
            )
        frame = result.frame
        stamped = _modal_day(frame["quoted_on"])
        actual = day or stamped or self.clock.today()
        written = self.snapshots.replace_day(
            market=market, day=actual, taken_at=taken_at, frame=frame
        )
        answered = set(frame["code"])
        return SnapshotSync(
            market=market,
            day=actual,
            taken_at=taken_at,
            rows=written,
            universe_size=len(wanted),
            skipped=tuple(code for code in wanted if code not in answered),
            source=result.source,
        )


def _modal_day(column: pl.Series) -> date | None:
    """众数日子：一只停牌票的旧时刻不改写这一天（D186）。平票取较晚的那天。"""
    days = column.drop_nulls().to_list()
    if not days:
        return None
    counts: dict[date, int] = {}
    for value in days:
        counts[value] = counts.get(value, 0) + 1
    best = max(counts.values())
    return max(day for day, count in counts.items() if count == best)
