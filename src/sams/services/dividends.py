"""分红入账（S4-D §6.2）。

只做一件事：把 `corp_actions` 里那些「除权日已到、那天之前手里有股」的派息记进现金流水。
没有派息日就按**除权日**入账，不编造一个；金额是**税前毛额**，A 股红利税
在卖出时按持有期补扣，v0 不建模，页面文案由 `TAX_NOTE` 说出这件事。

数量按**除权日前**（市场本地日）已成交的累计持股算，不是今天的持仓：除权日当天才买进来的
股不享有这次派息，卖光之后的除权日也不会凭空长出一笔。重跑同一区间靠 `ref`
（`market:code:ex_date`）跳过已入账的那些——它不是错误，是 0 行。

资格只看这个账户的**成交**，不看 `sim_accounts.created_at`（D294）：先把真实券商的
历史成交补记进一个新建的账户是支持的用法，那一天的除权日早于「我们按下新增按钮的时刻」，
派息仍然欠着。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sams.core import funds
from sams.core.codes import canonical_market
from sams.core.config import Settings
from sams.core.errors import StorageError
from sams.services.actors import SYSTEM_ACTOR
from sams.storage.repos.corp_actions import CorpActionRepo
from sams.storage.repos.ledger import Order, SimAccountRepo, SimOrderRepo

#: 屏幕上那句话的唯一出处：服务、CLI、页面读同一份，不各写一句略有出入的
TAX_NOTE = "税前毛额，未扣红利税"

_KIND = "dividend"


@dataclass(frozen=True)
class DividendPosting:
    market: str
    code: str
    ex_date: date
    qty: float
    cash_per_share: float
    #: 标的币种里的毛额：`qty × cash_per_share`
    amount: float
    currency: str


@dataclass(frozen=True)
class DividendReport:
    account_id: str
    postings: tuple[DividendPosting, ...] = ()
    #: 同一 `ref` 已经入过账的条数：重跑时它们不是错，只是没什么可做
    skipped_existing: int = 0
    #: 除权日之前持股为 0（或已卖光）的条数
    skipped_no_shares: int = 0
    warnings: tuple[str, ...] = ()

    @property
    def rows(self) -> int:
        return len(self.postings)


class DividendService:
    def __init__(
        self,
        accounts: SimAccountRepo,
        orders: SimOrderRepo,
        actions: CorpActionRepo,
        settings: Settings,
    ) -> None:
        self.accounts = accounts
        self.orders = orders
        self.actions = actions
        self.settings = settings

    def post(
        self, account_id: str, as_of: date, *, market: str | None = None
    ) -> DividendReport:
        """把截至 `as_of`（含当天）的应得派息入账。

        `as_of` 含当天：盘后跑的那一趟要能记上当天的除权日，补账时 `--as-of` 也是闭区间。
        `market` 只用于调度按市场分趟；不传就是所有已配置市场。
        """
        account = self.accounts.find(account_id)
        if account is None:
            raise StorageError(f"账户 {account_id} 不存在")
        by_code = _orders_by_code(self.orders.orders(account.id))
        wanted = [canonical_market(market)] if market else sorted(self.settings.markets)
        postings: list[DividendPosting] = []
        skipped_existing = skipped_no_shares = 0
        warnings: list[str] = []
        for key in wanted:
            zone = self.settings.market_cfg(key).zone()
            for row in self.actions.list_market(key, until=as_of).iter_rows(named=True):
                if row["kind"] != _KIND:
                    continue
                held = by_code.get((key, row["code"]))
                if held is None:
                    continue
                qty = _held_before(held, row["ex_date"], zone)
                if qty <= 0:
                    skipped_no_shares += 1
                    continue
                cash = row["cash_per_share"]
                if cash is None or cash <= 0:
                    warnings.append(
                        f"{key} {row['code']} {row['ex_date']} 的 dividend 行没有"
                        " cash_per_share（每股派息），跳过待补数"
                    )
                    continue
                ref = f"{key}:{row['code']}:{row['ex_date']}"
                if self.accounts.cash_ref_exists(account.id, kind=_KIND, ref=ref):
                    skipped_existing += 1
                    continue
                currency = funds.quote_currency(key, row["code"], self.settings.markets)
                amount = qty * cash
                moment = datetime.combine(
                    row["ex_date"], datetime.min.time(), tzinfo=zone
                ).replace(hour=15)
                self.accounts.adjust_cash(
                    account.id,
                    currency,
                    amount,
                    # 审计行要能独立复核：代码、除权日、每股、数量，缺一项就得回去查
                    reason=(
                        f"分红 {row['code']} 除权 {row['ex_date']} 每股 {cash:g}"
                        f" × {qty:g} 股（税前）"
                    ),
                    actor_id=SYSTEM_ACTOR,
                    ts=moment,
                    kind=_KIND,
                    ref=ref,
                )
                postings.append(
                    DividendPosting(
                        market=key,
                        code=row["code"],
                        ex_date=row["ex_date"],
                        qty=qty,
                        cash_per_share=cash,
                        amount=amount,
                        currency=currency,
                    )
                )
        return DividendReport(
            account_id=account.id,
            postings=tuple(postings),
            skipped_existing=skipped_existing,
            skipped_no_shares=skipped_no_shares,
            warnings=tuple(warnings),
        )


def _orders_by_code(orders: list[Order]) -> dict[tuple[str, str], list[Order]]:
    grouped: dict[tuple[str, str], list[Order]] = {}
    for order in orders:
        grouped.setdefault((order.market, order.code), []).append(order)
    return grouped


def _held_before(orders: list[Order], ex_date: date, zone) -> float:
    """除权日之前（市场本地日严格早于它）的累计持股。"""
    qty = 0.0
    for order in orders:
        if order.ts.astimezone(zone).date() < ex_date:
            qty += order.qty if order.side == "buy" else -order.qty
    return qty
