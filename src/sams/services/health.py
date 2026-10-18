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
    "corp_actions_coverage",
    "trading_calendar",
    "users",
    "user_sessions",
    "watchlists",
    # 基本面这三张表的行数只说明「库里有什么」，能不能拿它算 PR 要看下面的覆盖只数
    "financials",
    "valuation_daily",
    "funding_flow",
    "scan_results",
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
    #: 有 K 线但一条除权覆盖确认都没有的只数。这些票的跨除权日区间涨跌算不出来
    #: （读取层的守卫会拒绝），而它们在行数上看起来与别的票一模一样
    codes_without_actions: int = 0

    @property
    def amount_missing(self) -> int:
        return self.rows - self.with_amount


@dataclass(frozen=True)
class FundamentalsCoverage:
    """一个市场的基本面输入：三张表各按自己的粒度报「有几只票、最新到哪一天」。

    报**只数**而不是行数：`financials = 400 行` 可能是 200 只票两期，也可能是 2 只票
    两百期，而 `sams screen pr` 扫的是票——2 只与 200 只决定了那一篮子能不能叫「扫描」。
    最新日期也分粒度：财务的最新值是**报告期**（不是采集日，这一张表没有采集时间列），
    估值与资金流的最新值是**交易日**。
    """

    market: str
    financials_codes: int
    financials_latest_period: date | None
    #: 只数已知的最近披露日；源没给披露日的那些期不参与（NULL 不是「很久以前」）
    financials_latest_published: date | None
    valuation_codes: int
    valuation_latest: date | None
    flow_codes: int
    flow_latest: date | None


@dataclass(frozen=True)
class HealthReport:
    db_path: str
    tables: dict[str, int] = field(default_factory=dict)
    calendar_years: dict[str, list[int]] = field(default_factory=dict)
    sources: list[SourceRecord] = field(default_factory=list)
    pending_migrations: list[str] = field(default_factory=list)
    market_data: dict[str, MarketData] = field(default_factory=dict)
    fundamentals: dict[str, FundamentalsCoverage] = field(default_factory=dict)

    @property
    def degraded_sources(self) -> list[SourceRecord]:
        # 只认真正失败过：`ok is None` 是从未被路由到的源，把它算成故障会让
        # 「单源市场」永远显示为降级，而把真的降级淹没在噪音里
        return [s for s in self.sources if s.ok is False]

    @property
    def unverified_sources(self) -> list[SourceRecord]:
        return [s for s in self.sources if s.ok is None]


#: 三张基本面表各自的「有几只票、最新到哪一天」。列名到报告字段的映射写在同一行，
#: 因为漏掉一张表的代价是那一格永远显示 0 只，而 0 只看起来像「这个市场没数据」
_COVERAGE_QUERIES = (
    (
        "financials",
        """
        SELECT market, count(DISTINCT code) AS codes,
               max(report_date) AS latest_period, max(published_date) AS latest_published
        FROM financials GROUP BY market
        """,
        {
            "codes": "financials_codes",
            "latest_period": "financials_latest_period",
            "latest_published": "financials_latest_published",
        },
    ),
    (
        "valuation_daily",
        "SELECT market, count(DISTINCT code) AS codes, max(date) AS latest"
        " FROM valuation_daily GROUP BY market",
        {"codes": "valuation_codes", "latest": "valuation_latest"},
    ),
    (
        "funding_flow",
        "SELECT market, count(DISTINCT code) AS codes, max(date) AS latest"
        " FROM funding_flow GROUP BY market",
        {"codes": "flow_codes", "latest": "flow_latest"},
    ),
)


def _blank() -> dict:
    return {
        "financials_codes": 0,
        "financials_latest_period": None,
        "financials_latest_published": None,
        "valuation_codes": 0,
        "valuation_latest": None,
        "flow_codes": 0,
        "flow_latest": None,
    }


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
            fundamentals=self._fundamentals_coverage(),
        )

    def _fundamentals_coverage(self) -> dict[str, FundamentalsCoverage]:
        """按市场报「这条规则的输入到底有几只票」，不是「库里有几行」。

        没同步过的市场照样出现（全 0）：`sams screen pr` 在 HK 上扫不出东西，
        原因必须是「0 只」这个事实，而不是这一屏上看不见这一项。
        """
        tables = self.db.table_names()
        buckets = {m: _blank() for m in self.settings.markets}
        for table, sql, targets in _COVERAGE_QUERIES:
            if table not in tables:
                continue
            for row in self.db.query(sql, []).to_dicts():
                bucket = buckets.setdefault(row["market"], _blank())
                for column, target in targets.items():
                    bucket[target] = (
                        int(row[column]) if target.endswith("_codes") else _as_date(row[column])
                    )
        return {m: FundamentalsCoverage(market=m, **v) for m, v in sorted(buckets.items())}

    def _market_data(self) -> dict[str, MarketData]:
        """按市场汇总日线事实。

        只回答「库里有什么」，不回答「应该有什么」：最后一天是哪天算不算滞后，
        取决于该市场的交易日历和停牌，那条判断留给读的人（也是唯一看得见上下文的地方）。
        """
        if "bars_daily" not in self.db.table_names():
            return {}
        uncovered = self._codes_without_coverage()
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
                codes_without_actions=uncovered.get(r["market"], 0),
            )
            for r in rows
        }

    def _codes_without_coverage(self) -> dict[str, int]:
        """每个市场里有 K 线、却没有一条除权覆盖确认的只数。

        这一格是 D36 那一对的屏幕版本：`corp_actions` 的行数说的是「查到的记录」，
        而「这只票从没分过红」与「从没查过这只票」只有覆盖表能分清。不列出来，
        一库 30000 行看起来什么都有，实际上一半的票算不出区间涨跌。
        """
        tables = self.db.table_names()
        if "corp_actions_coverage" not in tables or "bars_daily" not in tables:
            return {}
        rows = self.db.query(
            """
            SELECT b.market AS market,
                   count(DISTINCT b.code) FILTER (WHERE c.code IS NULL) AS uncovered
            FROM bars_daily b
            LEFT JOIN corp_actions_coverage c ON b.market = c.market AND b.code = c.code
            WHERE b.adjust = ?
            GROUP BY b.market
            """,
            [ADJUST],
        ).to_dicts()
        return {r["market"]: int(r["uncovered"]) for r in rows}

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
