"""业绩归因（S5-B/C §3）：这段时间的收益是谁带来的，加起来不等于总收益就说清差多少。

四条口径决定这张表会不会骗人：

* **买入当天不计收益**：贡献用「前一天的持股数 × 当天的每股价值变化 ÷ 前一天的净值」，
  新进的那笔钱不是这段行情的功劳；卖出当天照计（清仓行由 NAV 重建留在库里，S5-A）；
* **缺口不跨**：窗口只取连续完整日，实际用了哪几天写在报告上，不拿首尾两点连线冒充；
* **分红归给派它的票**：到账现金按 `sim_cash_moves.ref`（`market:code:除权日`）记在那只票
  名下，不留在残差里——「这只票这段时间给了我多少钱」要包括它发现的金（D267）；
* **基准跟着这一段持仓的市场走**：账户开在 A 不等于它的钱压在 A。这一段跨两个市场、
  或基准缺其中任何一个净值日的收盘，就整段不给超额，只出绝对收益（D291/D292）；
* **算不出就说算不出**：不足两个完整日时整张表是空的并给原因——空的归因表比一张 0 更诚实。

`window_return` 用逐日链乘（与 S4-D 的 TWR 同一条），各票贡献是「日收益直接相加」，
两者天生差一个复利交叉项。这个差不藏：`residual` 连同来源一起印在卡上（D265）。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from dataclasses import replace as dataclass_replace
from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise

from sams.core.config import Settings
from sams.core.errors import StorageError
from sams.services.actors import resolve_actor
from sams.services.industry import IndustryService
from sams.services.nav import NAV_WINDOW_DAYS
from sams.storage.repos.holding_nav import HoldingNavPoint, HoldingNavRepo
from sams.storage.repos.index_daily import IndexDailyRepo
from sams.storage.repos.ledger import SimAccountRepo
from sams.storage.repos.nav import NavRepo
from sams.storage.repos.symbols import SymbolRepo
from sams.storage.repos.users import UserRepo

UNCLASSIFIED = "未分类"

#: 残差从哪几处来——印出来，让用户能核对「差这么多是合理的」
RESIDUAL_NOTE = (
    "残差来自：当日新建仓的价格效应、手续费、外部存取款的入账时点、"
    "没归到标的上的现金变动，以及复利交叉项（逐日直接相加与链乘本来的差额）"
)


@dataclass(frozen=True)
class HoldingContribution:
    market: str
    code: str
    name: str
    industry: str
    #: 区间内市值占净值的平均比例
    weight: float
    contribution: float
    #: 本币价格变动那一段（按前一天的汇率折回报告币种）
    price_part: float
    #: 汇率变动那一段；本币即报告币种时恒为 0
    fx_part: float
    #: 区间内到账的派息（折成报告币种后除以变动前一天的净值）
    dividend_part: float = 0.0


@dataclass(frozen=True)
class GroupContribution:
    label: str
    weight: float
    contribution: float


@dataclass(frozen=True)
class Benchmark:
    """大盘基准：区间内实际用的那两根收盘，以及它们算出来的涨跌。

    `value=None` 与 `value=0.0` 是两件事：前者是「取不到，超额不给」，
    后者是「跑平」——把前者印成后者，等于对着一份空库说「你这个季度没输」。
    """

    market: str
    index_code: str
    label: str
    start: date | None = None
    end: date | None = None
    value: float | None = None
    note: str = ""


@dataclass(frozen=True)
class AttributionReport:
    account_id: str
    currency: str
    start: date | None = None
    end: date | None = None
    days: int = 0
    window_return: float | None = None
    rows: tuple[HoldingContribution, ...] = ()
    industries: tuple[GroupContribution, ...] = ()
    markets: tuple[GroupContribution, ...] = ()
    residual: float | None = None
    residual_note: str = ""
    benchmark: Benchmark | None = None
    #: 区间收益 − 基准收益；基准取不到时是 `None`，不是 0
    excess: float | None = None
    #: 请求窗口里被丢掉的非连续完整日
    dropped_days: int = 0
    #: 非空 = 整张表算不出来，原因就在这里
    skipped: str = ""


class AttributionService:
    def __init__(
        self,
        users: UserRepo,
        accounts: SimAccountRepo,
        nav: NavRepo,
        holdings: HoldingNavRepo,
        industry: IndustryService,
        symbols: SymbolRepo,
        index: IndexDailyRepo,
        settings: Settings,
    ) -> None:
        self.users = users
        self.accounts = accounts
        self.nav = nav
        self.holdings = holdings
        self.industry = industry
        self.symbols = symbols
        self.index = index
        self.settings = settings

    def evaluate(self, username: str, account_id: str, start: date, end: date):
        user = resolve_actor(self.users, username)
        account = self.accounts.get(user.id, account_id)
        if account is None:
            raise StorageError(f"账户 {account_id} 不存在或不属于用户 {user.username}")
        points = self.nav.series(account.id, start, end)
        run = _run_ending_at_last_complete(points)
        if len(run) < 2:
            return AttributionReport(
                account_id=account.id,
                currency=account.report_currency,
                # 把账户 id 写进这句话：卡片与 CLI 原样转述，用户复制就能跑，
                # 留一个 `<账户>` 占位符等于让每个人自己回去查那个 id
                #
                # 先说盘后 job（D281）：净值不是「没人跑命令才没有」，`run_nav` 每晚按账户
                # 重建最近 400 天。只交一条 CLI 出去，等于让网页用户去等一个他看不见的
                # 手动步骤——同一页的净值卡（`partials/portfolio_nav.html`）说的就是这两层。
                skipped=(
                    f"这段时间还没有连续两个能算出来的净值日：盘后 job 每晚重建最近 "
                    f"{NAV_WINDOW_DAYS} 天，也可以跑 `sams nav rebuild --account "
                    f"{account.id}`；缺价/缺汇率的日子不并入"
                ),
                dropped_days=len(points) - len(run),
            )

        rows = self.holdings.series(account.id, run[0].day, run[-1].day)
        by_day: dict[date, dict[tuple[str, str], HoldingNavPoint]] = {}
        for row in rows:
            by_day.setdefault(row.day, {})[(row.market, row.code)] = row

        totals: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])
        weights: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0])
        dividends = _dividends_by_day(self.accounts, account.id, run[0].day, run[-1].day)
        for key, point in by_day.get(run[0].day, {}).items():
            if point.qty > 0:
                weights[key][0] += point.value_report / run[0].nav
                weights[key][1] += 1
        for previous, current in pairwise(run):
            held = by_day.get(previous.day, {})
            today = by_day.get(current.day, {})
            for key, before in held.items():
                if before.qty <= 0:
                    continue
                after = today.get(key)
                if after is None:
                    # 完整日里持仓还在却没行 = 取价规则变了，宁可不记也不猜
                    continue
                denominator = previous.nav
                totals[key][0] += (
                    before.qty * (after.price - before.price) * before.fx_rate / denominator
                )
                totals[key][1] += (
                    before.qty * after.price * (after.fx_rate - before.fx_rate) / denominator
                )
                if after.qty > 0:
                    weights[key][0] += after.value_report / current.nav
                    weights[key][1] += 1
            # 窗口起点的派息已经含在起始净值里，不是这段行情的收益，所以只收 run[1:]
            for key, amount in dividends.get(current.day, ()):
                point = today.get(key)
                if point is None:
                    continue
                totals[key][2] += amount * point.fx_rate / previous.nav

        entries = [
            HoldingContribution(
                market=market,
                code=code,
                name=self._name(market, code),
                industry=self.industry.industry_of(market, code) or UNCLASSIFIED,
                weight=(weights[key][0] / weights[key][1]) if weights[key][1] else 0.0,
                contribution=sum(totals[key]),
                price_part=totals[key][0],
                fx_part=totals[key][1],
                dividend_part=totals[key][2],
            )
            for key in totals
            for market, code in [key]
        ]
        entries.sort(key=lambda row: abs(row.contribution), reverse=True)
        window_return = _chain(run)
        total = sum(row.contribution for row in entries)
        benchmark = self._benchmark(_run_markets(entries), run)
        return AttributionReport(
            account_id=account.id,
            currency=account.report_currency,
            start=run[0].day,
            end=run[-1].day,
            days=len(run),
            window_return=window_return,
            rows=tuple(entries),
            industries=_group(entries, lambda row: row.industry),
            markets=_group(entries, lambda row: row.market),
            residual=window_return - total,
            residual_note=RESIDUAL_NOTE,
            benchmark=benchmark,
            excess=None if benchmark.value is None else window_return - benchmark.value,
            dropped_days=len(points) - len(run),
        )

    def _benchmark(self, markets: tuple[str, ...], run: list) -> Benchmark:
        """这一段压着哪个市场的钱，就对标那个市场的第一个配置指数。

        取不到就是取不到——`value=None` 一路带到卡片上是「—」，不是 0.0%。
        """
        if not markets:
            return Benchmark(market="", index_code="", label="", note="这一段没有持仓，超额不评")
        if len(markets) > 1:
            # 合成基准（A 70% + HK 30%）不做：宁可不给这个数，也不给一个说不出算法的数
            return Benchmark(
                market="",
                index_code="",
                label="",
                note=(
                    f"这一段持仓跨 {'、'.join(markets)} 共 {len(markets)} 个市场，"
                    "没有哪个单一指数配当这段的基准，只出绝对收益"
                ),
            )
        market = markets[0]
        codes = self.settings.indices.for_market(market)
        if not codes:
            return Benchmark(
                market=market,
                index_code="",
                label="",
                note=f"{market} 市场没有配置大盘基准，超额不给",
            )
        code = codes[0]
        label = self.settings.indices.label(code)
        closes = self.index.closes(market, code, start=run[0].day, end=run[-1].day)
        empty = Benchmark(market=market, index_code=code, label=label)
        if not closes:
            return dataclass_replace(
                empty,
                note=(
                    f"{label} 在 {run[0].day} ~ {run[-1].day} 没有收盘数据"
                    "（该市场无可用数据源，或指数日线未同步），超额不给"
                ),
            )
        first_day, last_day = run[0].day, run[-1].day
        # 同窗口（D292）：这一段每一个净值日都要有收盘，缺一天整段就不给。
        # 「缺几天按现有的连乘」给出来的数仍然叫「超额」，而它已经不代表这段时间
        by_day = dict(closes)
        missing = [point.day for point in run if point.day not in by_day]
        if missing:
            listed = "、".join(str(day) for day in missing[:3])
            tail = f" 等 {len(missing)} 天" if len(missing) > 3 else ""
            return dataclass_replace(
                empty,
                note=f"{label} 缺 {listed}{tail} 的收盘，这一段不完整，只出绝对收益",
            )
        first_close = by_day[first_day]
        if first_close <= 0:
            return dataclass_replace(
                empty,
                start=first_day,
                end=last_day,
                note=f"{label} 在 {first_day} 的收盘不是正数，超额不给",
            )
        return dataclass_replace(
            empty,
            start=first_day,
            end=last_day,
            value=by_day[last_day] / first_close - 1.0,
        )

    def _name(self, market: str, code: str) -> str:
        found = self.symbols.get(market, code)
        return str((found or {}).get("name") or code)


def _run_markets(entries: list[HoldingContribution]) -> tuple[str, ...]:
    """这一段压着哪几个市场的钱（D291）。

    基准要的是「这几天的钱在哪个市场」，而 `sim_accounts.home_market` 说的是开户主场：
    一个 A 股账户把仓位全换成港股时，后者会把上证指数的涨跌安到 HKD 仓位头上。
    """
    return tuple(sorted({row.market for row in entries}))


def _run_ending_at_last_complete(points) -> list:
    """从最后一个完整日往回走，只收连续的日子：缺口前后的行情不并成一段。"""
    complete = [point for point in points if point.complete and point.nav]
    if not complete:
        return []
    run = [complete[-1]]
    for point in reversed(complete[:-1]):
        if (run[-1].day - point.day).days == 1:
            run.append(point)
        else:
            break
    run.reverse()
    return run


def _chain(run) -> float:
    """逐日链乘（与 S4-D 的 TWR 同一条）：外部流入不算收益。"""
    total = 1.0
    for previous, current in pairwise(run):
        total *= 1.0 + (current.nav - current.external_flow - previous.nav) / previous.nav
    return total - 1.0


def _dividends_by_day(
    accounts: SimAccountRepo, account_id: str, start: date, end: date
) -> dict[date, tuple[tuple[tuple[str, str], float], ...]]:
    """按到账日归堆的派息：`(市场, 代码) → 标的币种里的毛额`。

    分红的钱是从标的币种进现金的（S4-D），`ref` 里的 `market:code` 就是它的出处；
    归堆按 UTC 日，与净值重放分现金的那一步同一条口径，否则同一天两边各数各的。
    """
    out: dict[date, list[tuple[tuple[str, str], float]]] = {}
    for row in accounts.cash_moves(account_id, until=_end_of(end)).iter_rows(named=True):
        if row["kind"] != "dividend":
            continue
        day = row["ts"].astimezone(UTC).date()
        if not start <= day <= end:
            continue
        parts = (row["ref"] or "").split(":")
        if len(parts) != 3:
            continue
        out.setdefault(day, []).append(((parts[0], parts[1]), float(row["delta"])))
    return {day: tuple(entries) for day, entries in out.items()}


def _end_of(day: date) -> datetime:
    """`day` 次日的 00:00Z：`cash_moves(until=)` 是闭区间到这一刻，等价于「含 day 当天」。"""
    return datetime.combine(day + timedelta(days=1), time.min, tzinfo=UTC)


def _group(entries, label_of) -> tuple[GroupContribution, ...]:
    buckets: dict[str, list[float]] = {}
    for row in entries:
        bucket = buckets.setdefault(label_of(row), [0.0, 0.0])
        bucket[0] += row.contribution
        bucket[1] += row.weight
    grouped = [
        GroupContribution(label=label, contribution=value[0], weight=value[1])
        for label, value in buckets.items()
    ]
    grouped.sort(key=lambda item: abs(item.contribution), reverse=True)
    return tuple(grouped)
