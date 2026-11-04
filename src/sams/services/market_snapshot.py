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
from sams.storage.repos.snapshots import CloseMismatch, SnapshotRepo
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


@dataclass(frozen=True)
class CloseAudit:
    """一晚的收盘对账：当天有没有见证快照，以及与它对不上的那些票。

    `witness_day=None` 与「有见证且零只不平」是两种答案，不能都写成 0：前者是这一晚
    什么都没比对，后者是比对过且相符。
    """

    witness_day: date | None
    mismatches: tuple[CloseMismatch, ...]
    #: 当天快照有价、bars_daily 里没那根、而这几天有线的（D310）。
    #: 与 `mismatches` 分开列：那一批是「数不对」，这一批是「整根没进来」，
    #: 排的是同一种活，但读数得能分清是哪种不平
    missing: tuple[str, ...] = ()


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

    def audit_closes(
        self, market: str, day: date, *, tolerance: float, within_days: int
    ) -> CloseAudit:
        """当天这一份全市场快照，与库里的日线逐只对一遍（D301）。

        见证是快照，不是另一个日线源：那一篮价格是盘后取的、不复权、覆盖全市场，
        而「库里这根 K 线是什么时候写的」没有第二处能回答。没有当天那一份时不拿
        前一天冒充——那会把「今天还没收盘」念成「今天的收盘价和昨天一样」。
        `missing` 是同一份见证问的第二句：内连接看不见「整根没进来」，而那是能补也该补的
        （D310）。要不要排由调用方判断，这里只把缺口列出来。
        """
        market = canonical_market(market)
        if self.snapshots.latest_until(market, day) != day:
            return CloseAudit(witness_day=None, mismatches=(), missing=())
        return CloseAudit(
            witness_day=day,
            mismatches=tuple(
                self.snapshots.close_mismatches(market, day, tolerance=tolerance)
            ),
            missing=tuple(self.snapshots.missing_bars(market, day, within_days=within_days)),
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
