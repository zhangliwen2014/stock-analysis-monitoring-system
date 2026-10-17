"""健康检查用例：监控系统监控自己（spec §12）。

只汇聚事实（库内行数、日历覆盖、各源最近错误、待应用迁移），
怎么排版展示是 cli 的事——这样将来接 Web 面板不必改这里。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sams.core.calendar import CalendarStore
from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.config import Settings
from sams.core.sources import SourceHealthView, SourceRecord
from sams.storage.db import Database
from sams.storage.migrate import pending

#: 健康报告里点名关注的表；其它表（M2/M3 才用到）只报存在与否
KEY_TABLES = (
    "symbols",
    "bars_daily",
    "trading_calendar",
    "users",
    "user_sessions",
    "watchlists",
)


@dataclass(frozen=True)
class HealthReport:
    db_path: str
    tables: dict[str, int] = field(default_factory=dict)
    calendar_years: dict[str, list[int]] = field(default_factory=dict)
    sources: list[SourceRecord] = field(default_factory=list)
    pending_migrations: list[str] = field(default_factory=list)

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
        )

    def _calendar_years(self) -> dict[str, list[int]]:
        if "trading_calendar" not in self.db.table_names():
            # 库还没迁移时健康检查更要能跑完：它正是用来发现这件事的命令
            return {}
        this_year = self.clock.today().year
        years = range(this_year - self.year_horizon, this_year + 2)
        return {
            market: [y for y in years if self.calendar_store.year_covered(market, y)]
            for market in self.settings.markets
        }
