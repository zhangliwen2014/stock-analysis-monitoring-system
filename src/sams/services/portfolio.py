"""持仓与账户的读用例（spec §5.4）。

写不在这个类里（`LedgerService`）：读用例不该有机会改账。

现价取自 `BarRepo`（库里只有不复权那一套，`adjust = 'raw'` 写死在查询里），而不是走
M2 那条后复权链：`avg_cost` 是成交那天的真价格，拿 hfq 绝对值去减它，算出来的是一段
从来没存在过的价差（D73）。换算锚在**行情的那一天**而不是「今天」，这样这个类不需要
墙钟，也避开了用 UTC 日期推交易日在美股上恒错一天（`core/clock.py` 开头那句警告）。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date

from sams.core import funds
from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.errors import StorageError
from sams.services.actors import resolve_actor
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.fx import FxRepo
from sams.storage.repos.ledger import (
    Account,
    Position,
    SimAccountRepo,
    SimOrderRepo,
)
from sams.storage.repos.users import User, UserRepo


@dataclass(frozen=True)
class PositionRow:
    market: str
    code: str
    currency: str
    #: 报告币种：`fx_pnl` 那一段的单位。它之前只在 `AccountSummary` 里有，所以屏幕上
    #: 那段数一直没人认领——标错了也没人看得出
    report_currency: str
    qty: float
    avg_cost: float
    last_price: float | None
    last_price_date: date | None
    #: None = 这一行给不出任何报告币种里的数（缺现价或缺汇率），原因在 `unavailable_note`
    pnl: funds.PositionPnl | None = None
    #: False = 不进汇总。它不是「数为 0」，而是「这一笔此刻无法换算」
    convertible: bool = False
    #: 给不出现价 / 换不出币时的一句话（显示与 `--json` 读同一句，别长两张嘴）
    unavailable_note: str | None = None

    @property
    def price_pnl(self) -> float | None:
        """标的币种里的价格盈亏；给不出可信拆分时是 None，不是 0。"""
        return None if self.pnl is None else self.pnl.price_pnl

    @property
    def fx_pnl(self) -> float:
        return 0.0 if self.pnl is None else self.pnl.fx_pnl

    @property
    def total_report(self) -> float:
        """报告币种里的市值−成本；不可换算的那一行贡献 0，并在 `unavailable_note` 里点名。"""
        return 0.0 if self.pnl is None else self.pnl.total_report

    @property
    def market_value_report(self) -> float:
        return 0.0 if self.pnl is None else self.pnl.market_report

    @property
    def cost_report(self) -> float:
        return 0.0 if self.pnl is None else self.pnl.cost_report

    @property
    def rate_base(self) -> float | None:
        return None if self.pnl is None else self.pnl.rate_base

    @property
    def rate_today(self) -> float | None:
        return None if self.pnl is None else self.pnl.rate_today

    @property
    def rate_stale(self) -> bool:
        return False if self.pnl is None else self.pnl.rate_stale


@dataclass(frozen=True)
class AccountSummary:
    """一个账户的汇总：本币小计分开、逐笔换算、换汇单列（§5.4 规则 2/3）。"""

    account_id: str
    name: str
    report_currency: str
    #: 各币种的现金余额，原样给出（本币数额，不做换算）
    cash_by_currency: dict[str, float]
    #: 各币种持仓的市值，取各行情日的收盘价；没有行情的那一行不进这里
    position_value_by_currency: dict[str, float]
    #: 报告币种里的总市值（现金 + 持仓）。换不出的那一笔不在里面，在 `notes` 里
    total_report: float
    price_pnl_report: float
    fx_pnl: float
    realized_report: float
    #: 换算锚在哪一天：持仓里最新的行情日，没有持仓才用钟（见 `_as_of`）
    as_of: date
    notes: tuple[str, ...]
    duplicate_holdings: tuple[str, ...]

    @property
    def total_pnl_report(self) -> float:
        return self.price_pnl_report + self.fx_pnl


class PortfolioService:
    def __init__(
        self,
        accounts: SimAccountRepo,
        orders: SimOrderRepo,
        bars: BarRepo,
        fx: FxRepo,
        users: UserRepo,
        *,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.accounts = accounts
        self.orders = orders
        self.bars = bars
        self.fx = fx
        self.users = users
        self.clock = clock

    def positions(self, username: str, account_id: str) -> list[PositionRow]:
        return self._positions(resolve_actor(self.users, username), account_id)

    def _positions(self, user: User, account_id: str) -> list[PositionRow]:
        """当前持仓逐行给出（已清仓的不出现）；报告币种取的是**账户**那一列。

        读也带归属：与 `repos/theses.py` 同形，不提供「查全量再由调用方过滤」的接口
        ——后者把隔离交给每一个调用方，总有一个忘记。
        """
        account = self._account(user, account_id)
        rows: list[PositionRow] = []
        for pos in self.orders.positions(account_id):
            if pos.qty <= 0:
                continue
            rows.append(self._one(account_id, pos, account.report_currency))
        return rows

    def _account(self, user: User, account_id: str) -> Account:
        """1 不是 2（D77）：`--account` 这个参数没写错，错的是库里没有（或不是你的）这笔账。

        写用例那边给的也是 1：同一个条件给两个退出码，脚本按哪个分支都是赌。
        """
        account = self.accounts.get(user.id, account_id)
        if account is None:
            raise StorageError(f"账户 {account_id} 不存在或不属于用户 {user.username}")
        return account

    def _one(self, account_id: str, pos: Position, report: str) -> PositionRow:
        base = PositionRow(
            market=pos.market,
            code=pos.code,
            currency=pos.currency,
            report_currency=report,
            qty=pos.qty,
            avg_cost=pos.avg_cost,
            last_price=None,
            last_price_date=None,
        )
        price, price_day = self._last_price(pos.market, pos.code)
        if price is None:
            return replace(
                base,
                unavailable_note=(
                    f"{pos.market} {pos.code} 取不到现价（库里没有行情），这一行不计入盈亏"
                ),
            )
        rate_base, rate_today, stale, note = self._rates(account_id, pos, price_day, report)
        if note is not None:
            return replace(
                base,
                last_price=price,
                last_price_date=price_day,
                unavailable_note=note,
            )
        return replace(
            base,
            last_price=price,
            last_price_date=price_day,
            pnl=funds.position_pnl(
                qty=pos.qty,
                avg_cost=pos.avg_cost,
                last_price=price,
                rate_base=rate_base,
                rate_today=rate_today,
                rate_today_stale=stale,
            ),
            convertible=True,
        )

    def _last_price(self, market: str, code: str) -> tuple[float | None, date | None]:
        """未复权收盘（D73）：`BarRepo` 的读取一律带 `adjust = 'raw'`，后复权序列不参与。"""
        latest = self.bars.latest_date(market, code)
        if latest is None:
            return None, None
        frame = self.bars.load(market, [code], start=latest, end=latest)
        if frame.is_empty():
            return None, None
        value = frame["close"][-1]
        return (None if value is None else float(value)), latest

    def _rates(
        self, account_id: str, pos: Position, price_day: date, report: str
    ) -> tuple[float | None, float | None, bool, str | None]:
        """(买入基准汇率, 行情日汇率, 是否陈旧, 换算不出时的一句话)。

        汇率取**行情日或之前**最近的一条，不取「今天」：把换算锚在已经有行情的那一天，
        就不需要墙钟（`FxRepo.latest` 超过窗口直接返回 None，宁缺毋陈旧）。
        换不出时给 note 而不给数：把一笔 USD 的数额当成 CNY 加进总市值，是一个看起来
        完全正常的错数。
        """
        if pos.currency == report:
            return None, None, False, None
        base = self._average_base_rate(account_id, pos)
        found = self.fx.latest(price_day, pos.currency, report)
        if found is None:
            return base, None, True, (
                f"{pos.market} {pos.code} 以 {pos.currency} 计价，取不到 {price_day} 或之前"
                f"可用的 {pos.currency}→{report} 汇率：这一行不计入汇总（可运行 `sams sync fx`"
                f"或手工补一条）"
            )
        if base is None:
            return None, found.rate, found.is_stale, (
                f"{pos.market} {pos.code} 的买入记录里没有基准汇率，无法把盈亏拆成"
                f"价格段与换汇段：这一行不计入汇总"
            )
        return base, found.rate, found.is_stale, None

    def _average_base_rate(self, account_id: str, pos: Position) -> float | None:
        """这笔持仓的基准汇率 = 各笔买入的 `fx_rate_ref` 按**股数**加权（§5.4 line 434）。

        权重是股数不是金额：`avg_cost` 是每股成本，恒等式两边都乘以它才对得上。
        少一笔已知的基准就整段不拆——拆错的 `fx_pnl` 比不拆更难查。
        """
        entries: list[tuple[float, float, bool]] = []
        for order in self.orders.orders(account_id):
            if order.market != pos.market or order.code != pos.code or order.side != "buy":
                continue
            if order.fx_rate_ref is None:
                return None
            entries.append((order.qty, order.fx_rate_ref, False))
        return funds.weighted_rate(entries)

    def summary(self, username: str, account_id: str) -> AccountSummary:
        """账户汇总：先按币种给出本币小计，再逐笔换算（§5.4 规则 2/3）。

        把所有币种的数额直接相加报成一个数，是比「算错」更隐蔽的错：它看着完全正常。
        所以换不出的那一笔不进合计，但一定在 `notes` 里点名。
        """
        user = resolve_actor(self.users, username)
        account = self._account(user, account_id)
        rows = self._positions(user, account_id)
        as_of = self._as_of(rows)
        notes: list[str] = []
        value_by_ccy: dict[str, float] = {}
        price_report = 0.0
        fx_total = 0.0
        positions_report = 0.0
        for row in rows:
            # 本币小计是「有几股、什么价」的事实，与能不能换算成报告币种无关
            if row.last_price is not None:
                value_by_ccy[row.currency] = (
                    value_by_ccy.get(row.currency, 0.0) + row.qty * row.last_price
                )
            if row.pnl is None:
                if row.unavailable_note is not None:
                    notes.append(row.unavailable_note)
                continue
            price_report += row.pnl.price_pnl * (row.pnl.rate_base or 1.0)
            fx_total += row.pnl.fx_pnl
            positions_report += row.pnl.market_report
        cash = self.accounts.cash_balances(account_id)
        cash_report = 0.0
        for ccy, amount in sorted(cash.items()):
            if amount == 0:
                continue
            cash_report += self._convert(
                account, ccy, amount, as_of, notes, f"现金 {amount:.2f} {ccy}"
            )
        return AccountSummary(
            account_id=account_id,
            name=account.name,
            report_currency=account.report_currency,
            cash_by_currency=cash,
            position_value_by_currency=value_by_ccy,
            total_report=positions_report + cash_report,
            price_pnl_report=price_report,
            fx_pnl=fx_total,
            realized_report=self._realized(account, as_of, notes),
            as_of=as_of,
            notes=tuple(notes),
            duplicate_holdings=self._duplicate_holdings(user.id, account_id),
        )

    def _as_of(self, rows: list[PositionRow]) -> date:
        """换算锚在哪一天：持仓行情日里最新的那个，没有持仓才问钟。

        持仓的换算根本不用它（各自锚在自己的行情日），用它的只有现金与已实现盈亏——
        那些没有「行情日」可言。这里读钟是为了不写 `date.today()`：重放要能钉住时间。
        """
        days = [row.last_price_date for row in rows if row.last_price_date is not None]
        return max(days) if days else self.clock.today()

    def _convert(
        self, account: Account, ccy: str, amount: float, as_of: date, notes: list[str], label: str
    ) -> float:
        """一笔本币数额换成报告币种；换不出就点名并贡献 0，不猜一个汇率。"""
        if ccy == account.report_currency:
            return amount
        found = self.fx.latest(as_of, ccy, account.report_currency)
        if found is None:
            notes.append(
                f"{label} 换不出 {account.report_currency}"
                f"（{as_of} 或之前没有可用的 {ccy}→{account.report_currency} 汇率），未计入合计"
            )
            return 0.0
        return amount * found.rate

    def _realized(self, account: Account, as_of: date, notes: list[str]) -> float:
        """已实现盈亏按币种汇总再换算：它算的是历史（`sim_orders` 重放），与当前持仓无关，
        所以已清仓的那几只也要算进来。"""
        by_ccy: dict[str, float] = {}
        for pos in self.orders.positions(account.id):
            amount = self.orders.realized_pnl(account.id, pos.market, pos.code)
            if amount == 0.0:
                continue
            by_ccy[pos.currency] = by_ccy.get(pos.currency, 0.0) + amount
        out = 0.0
        for ccy, amount in sorted(by_ccy.items()):
            out += self._convert(
                account, ccy, amount, as_of, notes, f"已实现 {amount:.2f} {ccy}"
            )
        return out

    def duplicate_holdings(self, username: str, account_id: str) -> tuple[str, ...]:
        return self._duplicate_holdings(resolve_actor(self.users, username).id, account_id)

    def _duplicate_holdings(self, user_id: str, account_id: str) -> tuple[str, ...]:
        """本账户里那些「在别的账户也有一份」的标的。

        库里不冲突（`sim_positions` 主键带 account_id），但读的人会以为一只票坏了：
        两个账户各有一笔 601088，两个成本，屏幕上同一只票两个价。
        """
        holders: dict[tuple[str, str], set[str]] = {}
        for account in self.accounts.list_for(user_id):
            for pos in self.orders.positions(account.id):
                if pos.qty > 0:
                    holders.setdefault((pos.market, pos.code), set()).add(account.name)
        mine = {
            (pos.market, pos.code)
            for pos in self.orders.positions(account_id)
            if pos.qty > 0
        }
        return tuple(
            f"{market} {code} 同时在 {'、'.join(sorted(names))} 里持有"
            for (market, code), names in sorted(holders.items())
            if len(names) > 1 and (market, code) in mine
        )
