"""健康检查用例：监控系统监控自己（spec §12）。

只汇聚事实（库内行数、日历覆盖、各市场的日线新鲜度与字段覆盖、各源最近错误、待应用迁移），
怎么排版展示是 cli 的事——这样将来接 Web 面板不必改这里。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from sams.core.calendar import CalendarStore
from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.config import Settings
from sams.core.sources import SourceHealthView, SourceRecord
from sams.storage.db import Database
from sams.storage.migrate import pending
from sams.storage.repos.bars import ADJUST


def _as_date(value) -> date | None:
    """DuckDB 的 max(date) 在不同版本下给 date 或 datetime，报告里只要日历日。"""
    if value is None:
        return None
    return value.date() if isinstance(value, datetime) else value


#: 健康报告里点名关注数据量的表；其它表（M2/M3 才用到）只报存在与否
KEY_TABLES = (
    "symbols",
    "bars_daily",
    "corp_actions",
    "trading_calendar",
    "users",
    "user_sessions",
    "watchlists",
)


@dataclass(frozen=True)
class MarketData:
    """一个市场的日线事实：行数、覆盖、新鲜度、字段覆盖率。

    行数单独看没有意义——`bars_daily = 30000` 既可能是 300 只票十年，也可能是 3 只票
    昨天刚停更。真正决定「现在能不能拿这些数据算规则」的是后三列。
    """

    market: str
    rows: int
    codes: int
    latest: date | None
    with_amount: int
    negative_volume: int

    @property
    def amount_missing(self) -> int:
        return self.rows - self.with_amount


@dataclass(frozen=True)
class HealthReport:
    db_path: str
    tables: dict[str, int] = field(default_factory=dict)
    calendar_years: dict[str, list[int]] = field(default_factory=dict)
    sources: list[SourceRecord] = field(default_factory=list)
    pending_migrations: list[str] = field(default_factory=list)
    market_data: dict[str, MarketData] = field(default_factory=dict)

    @property
    def degraded_sources(self) -> list[SourceRecord]:
        # 只认真正失败过：`ok is None` 是从未被路由到的源，把它算成故障会让
        # 「单源市场」永远显示为降级，而把真的降级淹没在噪音里
        return [s for s in self.sources if s.ok is False]

    @property
    def unverified_sources(self) -> list[SourceRecord]:
        return [s for s in self.sources if s.ok is None]


class HealthService:
    def __init__(
        self,
        db: Database,
        registry: SourceHealthView,
        calendar_store: CalendarStore,
        settings: Settings,
        *,
        year_horizon: int = 3,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.db = db
        self.registry = registry
        self.calendar_store = calendar_store
        self.settings = settings
        self.year_horizon = year_horizon
        self.clock = clock

    def report(self) -> HealthReport:
        tables = {
            name: self.db.row_count(name) for name in KEY_TABLES if name in self.db.table_names()
        }
        return HealthReport(
            db_path=self.db.path,
            tables=tables,
            calendar_years=self._calendar_years(),
            sources=self.registry.health(),
            pending_migrations=pending(self.db),
            market_data=self._market_data(),
        )

    def _market_data(self) -> dict[str, MarketData]:
        """按市场汇总日线事实。

        只回答「库里有什么」，不回答「应该有什么」：最后一天是哪天算不算滞后，
        取决于该市场的交易日历和停牌，那条判断留给读的人（也是唯一看得见上下文的地方）。
        """
        if "bars_daily" not in self.db.table_names():
            return {}
        rows = self.db.query(
            """
            SELECT market,
                   count(*) AS rows,
                   count(DISTINCT code) AS codes,
                   max(date) AS latest,
                   count(amount) AS with_amount,
                   count(*) FILTER (WHERE volume < 0) AS negative_volume
            FROM bars_daily WHERE adjust = ?
            GROUP BY market ORDER BY market
            """,
            [ADJUST],
        ).to_dicts()
        return {
            r["market"]: MarketData(
                market=r["market"],
                rows=int(r["rows"]),
                codes=int(r["codes"]),
                latest=_as_date(r["latest"]),
                with_amount=int(r["with_amount"]),
                negative_volume=int(r["negative_volume"]),
            )
            for r in rows
        }

    def _calendar_years(self) -> dict[str, list[int]]:
        if "trading_calendar" not in self.db.table_names():
            # 库还没迁移时健康检查更要能跑完：它正是用来发现这件事的命令
            return {}
        this_year = self.clock.today().year
        years = range(this_year - self.year_horizon, this_year + 2)
        today = self.clock.today()
        return {
            # through 只对「今年」有意义：今年还没过完，要求年末有行会永远判为未覆盖
            market: [
                y for y in years if self.calendar_store.year_covered(market, y, through=today)
            ]
            for market in self.settings.markets
        }
