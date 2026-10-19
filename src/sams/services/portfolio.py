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
from sams.core.errors import ValidationError
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.fx import FxRepo
from sams.storage.repos.ledger import Position, SimAccountRepo, SimOrderRepo


@dataclass(frozen=True)
class PositionRow:
    market: str
    code: str
    currency: str
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


class PortfolioService:
    def __init__(
        self,
        accounts: SimAccountRepo,
        orders: SimOrderRepo,
        bars: BarRepo,
        fx: FxRepo,
    ) -> None:
        self.accounts = accounts
        self.orders = orders
        self.bars = bars
        self.fx = fx

    def positions(self, user_id: str, account_id: str) -> list[PositionRow]:
        """当前持仓逐行给出（已清仓的不出现）；报告币种取的是**账户**那一列。

        读也带 user_id：与 `repos/theses.py` 同形，不提供「查全量再由调用方过滤」的接口
        ——后者把隔离交给每一个调用方，总有一个忘记。
        """
        account = self.accounts.get(user_id, account_id)
        if account is None:
            raise ValidationError(f"账户 {account_id} 不存在或不属于用户 {user_id}")
        rows: list[PositionRow] = []
        for pos in self.orders.positions(account_id):
            if pos.qty <= 0:
                continue
            rows.append(self._one(account_id, pos, account.report_currency))
        return rows

    def _one(self, account_id: str, pos: Position, report: str) -> PositionRow:
        base = PositionRow(
            market=pos.market,
            code=pos.code,
            currency=pos.currency,
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
