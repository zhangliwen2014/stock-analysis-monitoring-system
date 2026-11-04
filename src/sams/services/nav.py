"""组合净值、TWR 与最大回撤（S4-D §6.3）。

一天一行地重放：收盘持仓由成交按市场本地日累加，现金由 `sim_cash_moves` 累加，
两边都折成账户报告币。**算不出来就写 NULL**：缺价、缺汇率的那一天
`complete=false`，TWR 与回撤只在连续完整日上算，缺口前后各算各的。

两条容易写错的口径：

* 外部流入（存入 − 取出）不是收益。TWR 的每一日先把它从净值里扣掉再比；
* 回撤按 NAV 的峰谷，不按 TWR 的累计曲线——两者在有大额注资时长得完全不一样。

拆股/送转 v0 不动持仓股数（那是用户的台账）：区间里出现这三类行为时只返回一句
黄色提示，净值照着「股数没变」算，并明说它可能失真。
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol

from sams.core import funds
from sams.core.config import Settings
from sams.core.errors import StorageError, ValidationError
from sams.services.decision import trading_days_between
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.corp_actions import CorpActionRepo
from sams.storage.repos.fx import FxRepo
from sams.storage.repos.holding_nav import HoldingNavPoint, HoldingNavRepo
from sams.storage.repos.ledger import Account, Order, SimAccountRepo, SimOrderRepo
from sams.storage.repos.nav import NavPoint, NavRepo

#: 停牌也不该无限沿用最后一根收盘：超过 5 个交易日就记缺价，不拿旧价补出一条平滑曲线
STALE_TRADING_DAYS = 5

#: 重建与页面读的是同一个窗口：400 个日历日够覆盖一年的 TWR 与回撤
NAV_WINDOW_DAYS = 400

#: 进「送转/拆股未调整」那句黄提示的三类行为（v0 不改用户台账）
UNADJUSTED_KINDS = frozenset({"split", "bonus", "transfer"})

#: 只有这两类现金流水是外部流入；买入/卖出/手续费/分红/换汇都在组合内部转
_EXTERNAL_KINDS = frozenset({"deposit", "withdraw"})


class CalendarView(Protocol):
    """只问「日历能不能回答这一天」：净值重建不触发采集，与决策卡同一条边界。"""

    def known_status(self, market: str, day: date) -> bool | None: ...


class WeekdayCalendar:
    """没接日历时按工作日保守计：宁可早一点判超期，不晚判。"""

    def known_status(self, market: str, day: date) -> bool | None:
        return None


@dataclass(frozen=True)
class NavSeries:
    account_id: str
    currency: str
    days: tuple[NavPoint, ...]
    #: 区间内最后一个**完整**日：缺价的那天不会顶掉它
    latest: NavPoint | None
    #: 紧挨在 `latest` 前一天、且同样完整的那一天；隔了缺口就是 None
    previous: NavPoint | None
    #: 当日变动已扣掉当日外部流入：一次注资不该显示成「涨了」
    day_change: float | None
    day_change_pct: float | None
    twr: float | None
    twr_start: date | None
    #: 区间里算不出来的天数（缺价/缺汇率）
    gap_days: int
    #: 与 TWR 同一段连续完整日的 NAV 峰谷跌幅
    max_drawdown: float | None
    #: 当前净值相对同一段峰值的回撤：限额说的是「现在离高点多远」，不是区间最深的一坑
    current_drawdown: float | None
    split_warning: str | None


class NavService:
    def __init__(
        self,
        accounts: SimAccountRepo,
        orders: SimOrderRepo,
        bars: BarRepo,
        fx: FxRepo,
        actions: CorpActionRepo,
        calendar: CalendarView | None,
        settings: Settings,
        *,
        store: NavRepo | None = None,
    ) -> None:
        self.accounts = accounts
        self.orders = orders
        self.bars = bars
        self.fx = fx
        self.actions = actions
        self.calendar = calendar or WeekdayCalendar()
        self.settings = settings
        self.store = store if store is not None else NavRepo(accounts.db)
        self.holdings = HoldingNavRepo(accounts.db)

    def rebuild(self, account_id: str, start: date, end: date) -> int:
        """重建 `[start, end]` 每一天并整段覆盖旧行；返回写入天数。

        起点不早于「账户开立日与台账最早一笔事件里更早的那个」：建账按钮按下去之前
        回填的真实成交同样有这一段历史，剪掉它等于让曲线比持仓短一截（D294）。
        """
        account = self.accounts.find(account_id)
        if account is None:
            raise StorageError(f"账户 {account_id} 不存在")
        if end < start:
            return 0

        orders = self.orders.orders(account.id)
        moves_by_day, flows_by_day = self._cash_by_day(account.id, end)
        start = max(start, self._curve_start(account, orders, moves_by_day))
        if start > end:
            return 0

        by_code = _orders_by_code(orders)
        bar_series = self._load_bars(by_code, start, end)
        opened_before: dict[tuple[str, str], float] = {}
        orders_by_day: dict[date, list[tuple[tuple[str, str], float]]] = {}
        for order in orders:
            key = (order.market, order.code)
            local = order.ts.astimezone(self.settings.market_cfg(order.market).zone()).date()
            signed = order.qty if order.side == "buy" else -order.qty
            if local < start:
                opened_before[key] = opened_before.get(key, 0.0) + signed
            elif local <= end:
                orders_by_day.setdefault(local, []).append((key, signed))

        holdings = dict(opened_before)
        # 窗口之前动过的钱是**期初余额**：只重放窗口内的流水会把一笔存入漏掉，
        # 那条净值曲线整段低一个常数，看起来还是一条正常的线
        cash: dict[str, float] = {}
        for moved_day, entries in moves_by_day.items():
            if moved_day < start:
                for ccy, delta in entries:
                    cash[ccy] = cash.get(ccy, 0.0) + delta
        points: list[NavPoint] = []
        holding_rows: list[HoldingNavPoint] = []
        day = start
        while day <= end:
            held_yesterday = {key for key, qty in holdings.items() if qty > 0}
            for key, signed in orders_by_day.get(day, []):
                holdings[key] = holdings.get(key, 0.0) + signed
            for ccy, delta in moves_by_day.get(day, []):
                cash[ccy] = cash.get(ccy, 0.0) + delta
            point, rows = self._point(
                day,
                holdings=holdings,
                cash=cash,
                flows=flows_by_day.get(day, []),
                bars=bar_series,
                report=account.report_currency,
                # 今天清仓的票留一行 qty=0：卖出那天的涨跌要有归属，否则整段掉进归因残差
                closing={
                    key
                    for key, qty in holdings.items()
                    if qty <= 0 and key in held_yesterday
                },
            )
            points.append(point)
            holding_rows.extend(rows)
            day += timedelta(days=1)

        self.store.replace(account.id, start, end, points)
        self.holdings.replace(account.id, start, end, holding_rows)
        return len(points)

    def series(self, account_id: str, start: date, end: date) -> NavSeries:
        account = self.accounts.find(account_id)
        if account is None:
            raise StorageError(f"账户 {account_id} 不存在")
        points = self.store.series(account.id, start, end)
        return self._summarize(
            account.id,
            account.report_currency,
            points,
            start=start,
            end=end,
            orders=self.orders.orders(account.id),
        )

    # --- 这段历史从哪天开始 ---------------------------------------------------------------

    def curve_start(self, account_id: str, end: date) -> date:
        """这个账户的台账最早能从哪天算起：净值重放的起点，也是「全部」窗口的起点（D296）。

        「全部」要说的是「这个账户的全部历史」，而历史的起点由台账说话，不由建账按钮说话。
        两个读数各钳一次 `created_at`，就会一个说「全部」另一个少一段。
        """
        account = self.accounts.find(account_id)
        if account is None:
            raise StorageError(f"账户 {account_id} 不存在")
        moves_by_day, _ = self._cash_by_day(account.id, end)
        return self._curve_start(account, self.orders.orders(account.id), moves_by_day)

    # --- 重建用的小件 -------------------------------------------------------------------

    def _opened_on(self, opened_at: datetime, home_market: str) -> date:
        try:
            zone = self.settings.market_cfg(home_market).zone()
        except (StorageError, ValidationError, KeyError):
            zone = next(iter(self.settings.markets.values())).zone()
        return opened_at.astimezone(zone).date()

    def _curve_start(
        self,
        account: Account,
        orders: list[Order],
        moves_by_day: dict[date, list[tuple[str, float]]],
    ) -> date:
        """曲线最早能从哪天开始算：建账那天，或台账里最早的那一笔，取更早的（D294）。

        `sim_accounts.created_at` 记的是「什么时候按下了建账按钮」，不是「这个账户的钱
        从哪天开始有」。先建账、再回填真实券商历史是常见用法：拿 created_at 钳起点，
        开头那几天永远没有净值行，归因与回撤都少一段，屏幕上只看到曲线比持仓短。
        """
        days = list(moves_by_day)
        days.extend(
            order.ts.astimezone(self.settings.market_cfg(order.market).zone()).date()
            for order in orders
        )
        opened_on = self._opened_on(account.opened_at, account.home_market)
        return min([opened_on, *days]) if days else opened_on

    def _cash_by_day(
        self, account_id: str, end: date
    ) -> tuple[
        dict[date, list[tuple[str, float]]],
        dict[date, list[tuple[str, float]]],
    ]:
        """流水按 UTC 日分堆：成交与分红的时刻都在本地 15:00，换算后仍落在同一天。"""
        until = datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC)
        moves: dict[date, list[tuple[str, float]]] = {}
        flows: dict[date, list[tuple[str, float]]] = {}
        for row in self.accounts.cash_moves(account_id, until=until).iter_rows(named=True):
            day = row["ts"].astimezone(UTC).date()
            moves.setdefault(day, []).append((row["currency"], float(row["delta"])))
            if row["kind"] in _EXTERNAL_KINDS:
                flows.setdefault(day, []).append((row["currency"], float(row["delta"])))
        return moves, flows

    def _load_bars(
        self,
        by_code: dict[tuple[str, str], list[Order]],
        start: date,
        end: date,
    ) -> dict[tuple[str, str], tuple[list[date], list[float]]]:
        """每只票一次取完：重建是逐日的，但读库不该也是逐日的。

        往前多带 30 天：重建起点那天已经持有的票，它的最后一根收盘可能在区间之前；
        再旧就是「超过 5 个交易日」的缺价，不需要更远。
        """
        out: dict[tuple[str, str], tuple[list[date], list[float]]] = {}
        markets: dict[str, list[str]] = {}
        for market, code in by_code:
            markets.setdefault(market, []).append(code)
        for market, codes in markets.items():
            frame = self.bars.load(market, codes, start=start - timedelta(days=30), end=end)
            grouped: dict[str, tuple[list[date], list[float]]] = {}
            for row in frame.sort(["code", "date"]).iter_rows(named=True):
                days, closes = grouped.setdefault(row["code"], ([], []))
                days.append(row["date"])
                closes.append(float(row["close"]))
            for code in codes:
                out[(market, code)] = grouped.get(code, ([], []))
        return out

    def _point(
        self,
        day: date,
        *,
        holdings: dict[tuple[str, str], float],
        cash: dict[str, float],
        flows: list[tuple[str, float]],
        bars: dict[tuple[str, str], tuple[list[date], list[float]]],
        report: str,
        closing: set[tuple[str, str]] = frozenset(),
    ) -> tuple[NavPoint, list[HoldingNavPoint]]:
        reasons: list[str] = []
        stale = 0
        values: dict[str, float] = {}
        priced: list[tuple[str, str, str, float, float]] = []
        for (market, code), qty in holdings.items():
            closing_row = (market, code) in closing
            if qty <= 0 and not closing_row:
                continue
            days, closes = bars.get((market, code), ([], []))
            index = bisect_right(days, day) - 1
            if index < 0:
                # 清仓行拿不到价就不写这一行：它不该把这个账户的这一天判成算不出来
                if not closing_row:
                    reasons.append(f"{market} {code} 在 {day} 之前没有收盘价")
                continue
            age = trading_days_between(
                self.calendar, market, days[index], day, limit=STALE_TRADING_DAYS
            )
            if age > STALE_TRADING_DAYS:
                if not closing_row:
                    stale += 1
                    reasons.append(
                        f"{market} {code} 的收盘价停在 {days[index]}，"
                        f"已超过 {STALE_TRADING_DAYS} 个交易日"
                    )
                continue
            ccy = funds.quote_currency(market, code, self.settings.markets)
            if qty > 0:
                values[ccy] = values.get(ccy, 0.0) + qty * closes[index]
            priced.append((market, code, ccy, qty, closes[index]))

        flow = 0.0
        for ccy, amount in flows:
            rate = self._rate(ccy, report, day)
            if rate is None:
                reasons.append(f"缺 {ccy}→{report} 在 {day} 的汇率（外部流入）")
                continue
            flow += amount * rate

        cash_total = value_total = 0.0
        rates: dict[str, float] = {}
        for ccy in sorted(set(cash) | set(values)):
            rate = self._rate(ccy, report, day)
            if rate is None:
                reasons.append(f"缺 {ccy}→{report} 在 {day} 的汇率")
                continue
            rates[ccy] = rate
            cash_total += cash.get(ccy, 0.0) * rate
            value_total += values.get(ccy, 0.0) * rate

        if reasons:
            # 算不出来的那一天不写每票行：净值行本身已是 complete=false，
            # 归因只用完整日，不需要每只票再背一个状态列
            return (
                NavPoint(
                    day=day,
                    currency=report,
                    nav=None,
                    cash=None,
                    market_value=None,
                    external_flow=flow,
                    complete=False,
                    stale_prices=stale,
                    note="；".join(dict.fromkeys(reasons)),
                ),
                [],
            )
        rows = [
            HoldingNavPoint(
                day=day,
                market=market,
                code=code,
                qty=qty,
                price=price,
                fx_rate=rates[ccy],
                value_report=qty * price * rates[ccy],
            )
            for market, code, ccy, qty, price in priced
            # 清仓行所在的币种可能整天只有它一个：没有持仓要折算，就没必要为它取汇率
            if ccy in rates
        ]
        return (
            NavPoint(
                day=day,
                currency=report,
                nav=cash_total + value_total,
                cash=cash_total,
                market_value=value_total,
                external_flow=flow,
                complete=True,
                stale_prices=stale,
                note=None,
            ),
            rows,
        )

    def _rate(self, currency: str, report: str, day: date) -> float | None:
        found = self.fx.latest(day, currency, report)
        return None if found is None else found.rate

    # --- 读侧 ---------------------------------------------------------------------------

    def _summarize(
        self,
        account_id: str,
        currency: str,
        points: list[NavPoint],
        *,
        start: date,
        end: date,
        orders: list[Order],
    ) -> NavSeries:
        complete = [point for point in points if point.complete and point.nav is not None]
        latest = complete[-1] if complete else None
        previous: NavPoint | None = None
        if latest is not None:
            index = points.index(latest)
            if index > 0 and points[index - 1].complete and points[index - 1].nav is not None:
                previous = points[index - 1]
        change = pct = None
        if latest is not None and previous is not None and previous.nav:
            change = latest.nav - previous.nav - latest.external_flow
            pct = change / previous.nav

        run = self._run_ending_at(points, latest)
        twr, twr_start = _twr(run)
        drawdown = _max_drawdown(run)
        return NavSeries(
            account_id=account_id,
            currency=currency,
            days=tuple(points),
            latest=latest,
            previous=previous,
            day_change=change,
            day_change_pct=pct,
            twr=twr,
            twr_start=twr_start,
            gap_days=sum(1 for point in points if not point.complete),
            max_drawdown=drawdown,
            current_drawdown=_current_drawdown(run),
            split_warning=self._split_warning(orders, start, end),
        )

    @staticmethod
    def _run_ending_at(points: list[NavPoint], latest: NavPoint | None) -> list[NavPoint]:
        """从 `latest` 往前收连续完整日：隔一天不完整就停，缺口不当 0 收益。

        净值不是正数的那天也停（D297）：回撤与 TWR 都是拿上一日净值当分母的比率，
        负分母上连乘出来的仍然印成「最大回撤」，量级越离谱越像结论。
        """
        if latest is None or not _usable_as_base(latest.nav):
            return []
        index = points.index(latest)
        run = [latest]
        cursor = index
        while cursor > 0:
            candidate = points[cursor - 1]
            if (
                not candidate.complete
                or not _usable_as_base(candidate.nav)
                or candidate.day != points[cursor].day - timedelta(days=1)
            ):
                break
            run.append(candidate)
            cursor -= 1
        return list(reversed(run))

    def _split_warning(self, orders: list[Order], start: date, end: date) -> str | None:
        held = {(order.market, order.code) for order in orders}
        hits: list[str] = []
        for market in sorted({key[0] for key in held}):
            codes = {code for m, code in held if m == market}
            frame = self.actions.list_market(market, until=end)
            for row in frame.iter_rows(named=True):
                if (
                    row["kind"] in UNADJUSTED_KINDS
                    and row["code"] in codes
                    and start <= row["ex_date"] <= end
                ):
                    hits.append(f"{market} {row['code']}")
        if not hits:
            return None
        return (
            f"本区间有送转/拆股（{'、'.join(dict.fromkeys(hits))}），"
            "持仓股数未调整，净值可能失真"
        )


def _usable_as_base(nav: float | None) -> bool:
    """这天能不能当百分比的分母：只有正净值能（D297）。

    「先买后入金」的账户在回填历史后会有这样一段——钱还没进来、仓位已经有了，净值是负的。
    负分母上的比率仍然会被印成「收益率/回撤」，而越离谱越像结论。
    """
    return nav is not None and nav > 0


def _orders_by_code(orders: list[Order]) -> dict[tuple[str, str], list[Order]]:
    grouped: dict[tuple[str, str], list[Order]] = {}
    for order in orders:
        grouped.setdefault((order.market, order.code), []).append(order)
    return grouped


def _twr(run: list[NavPoint]) -> tuple[float | None, date | None]:
    """连续完整日上的时间加权收益：`(NAV_t − F_t) / NAV_{t−1} − 1` 逐日连乘。

    进来的 `run` 已经只留正净值的日子（`_run_ending_at`，D297），这里不再判分母：
    同一条规则写两遍，改一遍漏一遍。
    """
    if len(run) < 2:
        return None, run[0].day if run else None
    growth = 1.0
    for previous, current in zip(run, run[1:], strict=False):
        growth *= (current.nav - current.external_flow) / previous.nav
    return growth - 1.0, run[0].day


def _max_drawdown(run: list[NavPoint]) -> float | None:
    peak: float | None = None
    worst = 0.0
    seen = False
    for point in run:
        assert point.nav is not None
        if peak is None or point.nav > peak:
            peak = point.nav
        if peak > 0:
            worst = max(worst, (peak - point.nav) / peak)
            seen = True
    return worst if seen else None


def _current_drawdown(run: list[NavPoint]) -> float | None:
    """当前净值离这段连续完整日的高点有多远；净值不为正时算不出（不给 0）。"""
    if not run:
        return None
    peak = max(point.nav for point in run if point.nav is not None)
    latest = run[-1].nav
    if peak <= 0 or latest is None:
        return None
    return (peak - latest) / peak
