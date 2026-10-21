"""健康检查用例：监控系统监控自己（spec §12）。

只汇聚事实（库内行数、日历覆盖、各市场的日线新鲜度与字段覆盖、各源最近错误、待应用迁移），
怎么排版展示是 cli 的事——这样将来接 Web 面板不必改这里。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime

from sams.core.calendar import CalendarStore
from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.config import Settings
from sams.core.funds import FX_STALE_DAYS
from sams.core.notify import FAILED, KINDS, NO_CHANNEL, SENT, SUPPRESSED
from sams.core.sources import SourceHealthView, SourceRecord
from sams.storage.db import Database
from sams.storage.migrate import pending
from sams.storage.repos.bars import ADJUST
from sams.storage.repos.fx import FxPair, FxRepo
from sams.storage.repos.ledger import SimAccountRepo, SimOrderRepo


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
    # 通知这一路的三张表：通道是配置、alerts 是事实、notify_log 是结果
    "channels",
    "alerts",
    "notify_log",
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
class LedgerData:
    """账本那一节：这套系统有没有人在记账，以及换汇影响拆不拆得出来。

    三个数分开报。「只有订单没有持仓」（全卖了）与「一笔都没记」是两件事，合成一个
    「账本行数」就分不出来了。`fx` 按币种对列而不给一个全库最新日：见 `FxRepo.freshness`。
    """

    accounts: int = 0
    orders: int = 0
    #: 还持有的只数（不含清仓后留下的 0 股行）
    positions: int = 0
    fx: tuple[FxPair, ...] = ()

    @property
    def stale_pairs(self) -> tuple[FxPair, ...]:
        """超过 `FX_STALE_DAYS` 的那几个对：长假第五天还在用周五那个数就是假数（D87）。"""
        return tuple(p for p in self.fx if p.age_days > FX_STALE_DAYS)


@dataclass(frozen=True)
class NotifyData:
    """通知这一节：有几条通道、四种结果各发生过几次、本机那两个通道发得出去吗。

    `channels=None` 表示「库里还不是 0015 的形状」，与 0 是两句话：0 意思是「一个通道
    都没配」（用户该去配），None 意思是「该跑迁移」。把 None 写成 0，运维会去查
    「为什么通道没了」而真正的问题是版本。六个数**一起**可空：只让前两列可空的话，
    「0 次投递」这一句在未迁移的库上仍然是谎话。
    """

    channels: int | None = None
    enabled: int | None = None
    #: 四态各一个数（§11）。压制与「没有通道」分开数：那两个的修法不一样。
    sent: int | None = None
    failed: int | None = None
    suppressed: int | None = None
    no_channel: int | None = None
    #: 未处理（`status='new'`）的条数与**最早**那一条的时刻。跨全部用户数：健康页是运维视角，
    #: 「响了多少没人看」与「谁该看」是两件事。`None` 沿用上面那几个数的同一条线（D93）
    unhandled: int | None = None
    unhandled_oldest: datetime | None = None
    #: 本机那两个通道的可用性：**None 是「没接入探测」**（这一层不认识 monitor），
    #: 空串是「这台机器发得出去」，其余就是那一句原因。三个状态必须分得开——
    #: 把「没接入」显示成「可用」，健康页就在替一个从没试过的通道打包票。
    desktop: str | None = None
    voice: str | None = None
    #: 库里有、工厂不认识的 kind：路由会静默跳过，所以必须在这里出声
    unknown_kinds: tuple[str, ...] = ()


@dataclass(frozen=True)
class HealthReport:
    db_path: str
    tables: dict[str, int] = field(default_factory=dict)
    calendar_years: dict[str, list[int]] = field(default_factory=dict)
    sources: list[SourceRecord] = field(default_factory=list)
    pending_migrations: list[str] = field(default_factory=list)
    market_data: dict[str, MarketData] = field(default_factory=dict)
    fundamentals: dict[str, FundamentalsCoverage] = field(default_factory=dict)
    #: 没接线时也给一节全 0，而不是让这一节从屏幕上消失：缺席会被读成「账本没在用」
    ledger: LedgerData = field(default_factory=LedgerData)
    #: 未迁移的库里这一节全是 None（不是 0）：见 `NotifyData`
    notify: NotifyData = field(default_factory=NotifyData)

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
        sim_accounts: SimAccountRepo | None = None,
        sim_ledger: SimOrderRepo | None = None,
        fx: FxRepo | None = None,
        local_status: Callable[[], dict[str, str]] | None = None,
    ) -> None:
        self.db = db
        self.registry = registry
        self.calendar_store = calendar_store
        self.settings = settings
        self.year_horizon = year_horizon
        self.clock = clock
        #: 账本那一节的三个读数（仓库不传就是全 0，见 `LedgerData`）。
        #: 带 `sim_` 前缀是因为 `accounts` 在别的类里指用户账号服务，两个都对上名字必被误用
        self.sim_accounts = sim_accounts
        self.sim_ledger = sim_ledger
        self.fx = fx
        #: 「本机发得出去吗」那两句话由 monitor 探测，这里只转述：services 不许 import
        #: monitor（守卫），所以来的是一个函数而不是一个 monitor 对象。不传就是空串，
        #: 屏幕上那一行会写「未接入」而不是「可用」
        self.local_status = local_status

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
            ledger=self._ledger_data(),
            notify=self._notify_data(),
        )

    def _notify_data(self) -> NotifyData:
        """通道数 + 四态计数 + 积压 + 本机那两个通道的可用性 + 工厂不认识的 kind。

        积压跨全部用户：这一屏要回答的是「这套系统在响而有没有人看」，不是「你名下有几条」。
        「最早那一条」排在「条数」后面是因为只有那个数说不清紧急程度——40 条与 40 天是两回事。
        """
        names = set(self.db.table_names())
        if not {"channels", "alerts", "notify_log"} <= names:
            return NotifyData()
        if "outcome" not in self.db.columns("notify_log"):
            # 护栏在 SQL 之前：不许对一张不存在的列发查询。`channels`/`notify_log` 是 0001
            # 就有的表，所以「表在不在」不足以决定——0015 缺的那一列才是真正没跑到的东西
            return NotifyData()
        counts = {
            str(row["o"]): int(row["n"])
            for row in self.db.query(
                "SELECT coalesce(outcome, 'failed') AS o, count(*) AS n"
                " FROM notify_log GROUP BY o"
            ).to_dicts()
        }
        kinds = {
            str(row["kind"])
            for row in self.db.query("SELECT DISTINCT kind FROM channels").to_dicts()
        }
        status = self.local_status() if self.local_status is not None else {}
        unhandled = int(
            self.db.scalar("SELECT count(*) FROM alerts WHERE status = 'new'") or 0
        )
        oldest = (
            self.db.scalar("SELECT min(triggered_at) FROM alerts WHERE status = 'new'")
            if unhandled
            else None
        )
        return NotifyData(
            channels=int(self.db.scalar("SELECT count(*) FROM channels") or 0),
            enabled=int(self.db.scalar("SELECT count(*) FROM channels WHERE enabled") or 0),
            sent=counts.get(SENT, 0),
            failed=counts.get(FAILED, 0),
            suppressed=counts.get(SUPPRESSED, 0),
            no_channel=counts.get(NO_CHANNEL, 0),
            unhandled=unhandled,
            unhandled_oldest=oldest,
            desktop=status.get("desktop"),
            voice=status.get("voice"),
            unknown_kinds=tuple(sorted(kinds - set(KINDS))),
        )

    def _ledger_data(self) -> LedgerData:
        """账户/成交/持仓三个数 + 每个币种对的新鲜度。

        这里不写 SQL：三个数各自的口径（持仓不含 0 股行、汇率按对各取最后一天）都在仓库里，
        在健康页重抄一遍的话，先失效的是这一屏——它会报一个账本查询报不出来的数。

        表在不在要先问：0014 之前的老库照样要出这一节，那三个数就是 0，而不是当场炸——
        `sams health` 是「先看看这个库怎么了」的入口，它不能是第一个倒下的
        （`_fundamentals_coverage` 同此）。
        """
        tables = set(self.db.table_names())
        ledger_ready = (
            self.sim_accounts is not None
            and self.sim_ledger is not None
            and {"sim_accounts", "sim_orders", "sim_positions"} <= tables
        )
        fx_ready = self.fx is not None and "fx_rates" in tables
        return LedgerData(
            accounts=self.sim_accounts.count() if ledger_ready else 0,
            orders=self.sim_ledger.count_orders() if ledger_ready else 0,
            positions=self.sim_ledger.count_holdings() if ledger_ready else 0,
            fx=self.fx.freshness(self.clock.today()) if fx_ready else (),
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
