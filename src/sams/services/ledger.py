"""账本的写用例（spec §5.4）。

三件事只在这一层做：用户名解析成库里的 id（`services/actors.py`，与 thesis 认的是同一个 id，
否则 `--thesis` 那条链两边各认各的）、这笔账是不是你的（仓库层的 `_require` 不查归属）、
以及成交能不能挂在一条还没建仓的立论上（问的是状态机，它在 `repos/theses.py` 的
`TRANSITIONS` 里，D81）。币种在这里按配置推定、落库前由仓库校验，调用方不参与。

不 publish 事件：M3-A 没有消费者（M3-C 的规则引擎才会订 `QuoteUpdated`），先挂一个
没人听的 `publish` 会让「这个事件有没有人吃」重新变成要靠读代码猜的事。
"""

from __future__ import annotations

from datetime import date, datetime, time

from sams.core import funds
from sams.core.codes import canonical_code, canonical_market
from sams.core.config import Settings
from sams.core.errors import StorageError, ValidationError
from sams.core.version import VersionGate
from sams.services.actors import SYSTEM_ACTOR, resolve_actor
from sams.storage.repos.fx import FxRepo
from sams.storage.repos.ledger import (
    BUY,
    Account,
    FxTrade,
    Order,
    Position,
    SimAccountRepo,
    SimOrderRepo,
    settled_amount,
)
from sams.storage.repos.theses import OBSERVING, PROPOSED, ThesisRepo
from sams.storage.repos.users import User, UserRepo

#: 能挂成交的立论状态：还没建仓的不能挂——那会在时间线上留下一个从未发生的「我当时买了」。
#: 归档/失效的那两条能挂：「事后回头看，这笔是按当时的立论买的」是真实发生过的事。
_UNBORN = (OBSERVING, PROPOSED)

#: 占位汇率：D213 之前录进去的那个值。它不是汇率，是把标的币种当成了报告币种
_PLACEHOLDER_RATE = 1.0


class LedgerService:
    def __init__(
        self,
        accounts: SimAccountRepo,
        orders: SimOrderRepo,
        theses: ThesisRepo,
        users: UserRepo,
        settings: Settings,
        *,
        versions: VersionGate | None = None,
        fx: FxRepo | None = None,
    ) -> None:
        self.accounts = accounts
        self.orders = orders
        self.theses = theses
        self.users = users
        self.settings = settings
        #: 盘中缓存的失效闸（§5.4）：成交与现金都读得到「现在有多少」。可选，
        #: 理由见 `RuleService.versions`
        self.versions = versions
        #: 回算占位汇率要按成交日取汇率（D293）：同一个库上的读模型，不另找构造点
        self.fx = fx if fx is not None else FxRepo(accounts.db)

    def _bump(self) -> None:
        """写面动过就 bump：盘中缓存整表失效（§5.4）。没接闸的构造点静默跳过。"""
        if self.versions is not None:
            self.versions.bump("ledger")

    def open_account(
        self,
        username: str,
        name: str,
        *,
        broker: str | None = None,
        market_scope: tuple[str, ...] = (),
        margin_enabled: bool = False,
        home_market: str = "A",
    ) -> Account:
        user = resolve_actor(self.users, username)
        home_market = canonical_market(home_market)
        if home_market not in self.settings.markets:
            raise ValidationError(
                f"账户的主场市场 {home_market} 没有配置：可选 "
                f"{', '.join(sorted(self.settings.markets))}"
            )
        account = self.accounts.open(
            user.id,
            name,
            broker=broker,
            market_scope=market_scope,
            margin_enabled=margin_enabled,
            report_currency=self.settings.report_currency,
            home_market=home_market,
        )
        self._bump()
        return account

    def rename_account(self, username: str, account_id: str, name: str) -> Account:
        """改名不是开新户：持仓、成交、现金都挂 id，改完还是同一笔账（D204）。"""
        user = resolve_actor(self.users, username)
        self._require_account(user, account_id)
        account = self.accounts.rename(user.id, account_id, name)
        self._bump()
        return account

    def undo_fill(self, username: str, account_id: str, order_id: str) -> bool:
        """录错市场/代码的唯一出口：这笔成交从未发生，现金与持仓一起回到它之前（D205）。"""
        user = resolve_actor(self.users, username)
        self._require_account(user, account_id)
        undone = self.orders.undo_fill(account_id, order_id, actor_id=user.id)
        self._bump()
        return undone

    def repair_placeholder_rates(
        self, account_id: str, as_of: date, *, market: str | None = None
    ) -> int:
        """把 D213 之前录进去的占位汇率按成交日的汇率回算，返回修好的笔数（D293）。

        这种行今天的写入口已经录不出来（`record_fill` 拒 1.0），可库里那几行还带着它，
        而后果是持仓上凭空多出一段换汇亏损。把它交给用户「撤销重录」等于让系统自己的
        历史脏数据变成别人的手工活（与 D283 同一口径：数据不全由后台补）。

        `market` 是盘后链的形状：三个市场各跑一趟，各管本市场的成交（同分红入账）。
        两个不动的条件：成交日在汇率表里取不到（含退到最近一条也超期），或补差会把口袋
        扣穿。都不整笔动、留给下一晚——汇率同步补齐或用户入金的那一晚它自然就好了。
        """
        account = self.accounts.find(account_id)
        if account is None:
            raise StorageError(f"账户 {account_id} 不存在")
        wanted = canonical_market(market) if market is not None else None
        repaired = 0
        for order in self.orders.orders(account_id):
            recorded = order.fx_rate_applied
            if recorded != _PLACEHOLDER_RATE or order.currency == account.report_currency:
                continue
            if wanted is not None and order.market != wanted:
                continue
            zone = self.settings.market_cfg(order.market).zone()
            day = order.ts.astimezone(zone).date()
            if day > as_of:
                continue
            fx = self.fx.latest(day, order.currency, account.report_currency)
            if fx is None or fx.rate == _PLACEHOLDER_RATE:
                continue
            args = (order.side, order.qty, order.price, order.fee)
            delta = settled_amount(*args, fx.rate) - settled_amount(*args, recorded)
            reason = (
                f"回算 {day} 那笔 {order.market} {order.code} "
                f"{'买入' if order.side == BUY else '卖出'}的结算汇率："
                f"{recorded:.4f} → {fx.rate:.4f}（当日 {order.currency}→"
                f"{account.report_currency}），{account.report_currency} 口袋补 {delta:+,.2f}"
            )
            try:
                self.orders.repave_rate(
                    account_id,
                    order.id,
                    rate=fx.rate,
                    reason=reason,
                    actor_id=SYSTEM_ACTOR,
                    # 补在成交那天：余额是重算出来的，曲线也要从那一天起才对
                    ts=datetime.combine(day, time(15, 0), tzinfo=zone),
                )
            except StorageError:
                continue
            repaired += 1
        self._bump()
        return repaired

    def convert(
        self,
        username: str,
        account_id: str,
        from_ccy: str,
        to_ccy: str,
        amount: float,
        rate: float,
    ) -> FxTrade:
        """浏览器里的换汇：港股通先换 HKD 再买，汇率按用户填的成交价（D207）。"""
        user = resolve_actor(self.users, username)
        self._require_account(user, account_id)
        trade = self.accounts.convert(
            account_id, from_ccy, to_ccy, amount, rate, actor_id=user.id
        )
        self._bump()
        return trade

    def deposit(
        self,
        account_id: str,
        currency: str,
        amount: float,
        *,
        username: str,
        reason: str,
        actor_id: str,
    ) -> float:
        self._require_account(resolve_actor(self.users, username), account_id)
        balance = self.accounts.adjust_cash(
            account_id,
            currency,
            abs(amount),
            reason=reason,
            actor_id=actor_id,
            kind="deposit",
        )
        self._bump()
        return balance

    def withdraw(
        self,
        account_id: str,
        currency: str,
        amount: float,
        *,
        username: str,
        reason: str,
        actor_id: str,
    ) -> float:
        self._require_account(resolve_actor(self.users, username), account_id)
        balance = self.accounts.adjust_cash(
            account_id,
            currency,
            -abs(amount),
            reason=reason,
            actor_id=actor_id,
            kind="withdraw",
        )
        self._bump()
        return balance

    def buy(
        self,
        account_id: str,
        username: str,
        market: str,
        code: str,
        *,
        qty: float,
        price: float,
        fee: float = 0.0,
        thesis_id: str | None = None,
        fx_rate: float | None = None,
        ts: datetime | None = None,
    ) -> Order:
        return self._fill(
            account_id, username, market, code, side="buy", qty=qty, price=price, fee=fee,
            thesis_id=thesis_id, fx_rate=fx_rate, ts=ts,
        )

    def sell(
        self,
        account_id: str,
        username: str,
        market: str,
        code: str,
        *,
        qty: float,
        price: float,
        fee: float = 0.0,
        thesis_id: str | None = None,
        fx_rate: float | None = None,
        ts: datetime | None = None,
    ) -> Order:
        return self._fill(
            account_id, username, market, code, side="sell", qty=qty, price=price, fee=fee,
            thesis_id=thesis_id, fx_rate=fx_rate, ts=ts,
        )

    def _fill(
        self,
        account_id: str,
        username: str,
        market: str,
        code: str,
        *,
        side: str,
        qty: float,
        price: float,
        fee: float,
        thesis_id: str | None,
        fx_rate: float | None,
        ts: datetime | None,
    ) -> Order:
        user = resolve_actor(self.users, username)
        self._require_account(user, account_id)
        market = canonical_market(market)
        code = canonical_code(market, code)
        # 未配置的市场在这里就拒：它的币种推不出来（同 D84 的立论闸门），
        # 而且此时还没开事务，报错不会留下半笔账
        currency = funds.quote_currency(market, code, self.settings.markets)
        if thesis_id is not None:
            self._check_thesis(user, thesis_id)
        order = self.orders.record_fill(
            account_id=account_id,
            user_id=user.id,
            market=market,
            code=code,
            side=side,
            qty=qty,
            price=price,
            currency=currency,
            fee=fee,
            thesis_id=thesis_id,
            fx_rate=fx_rate,
            ts=ts,
        )
        self._bump()
        return order

    def _require_account(self, user: User, account_id: str) -> Account:
        """这笔账是不是你的：仓库层的 `_require` 只查存在、不查归属。

        不是 ValidationError：`--account` 这个参数本身没写错，错的是库里没有（或不是你的）
        这么一笔账——那是业务拒绝（退出码 1，D77）。
        """
        account = self.accounts.get(user.id, account_id)
        if account is None:
            raise StorageError(f"账户 {account_id} 不存在或不属于用户 {user.username}")
        return account

    def _check_thesis(self, user: User, thesis_id: str) -> None:
        thesis = self.theses.get(user.id, thesis_id)
        if thesis is None:
            # 不存在与不属于同一个回答（`ThesisRepo.get` 按 user_id 查，拿不到内容）
            raise ValidationError(f"立论 {thesis_id} 不存在或不属于用户 {user.username}")
        if thesis.status in _UNBORN:
            raise ValidationError(
                f"立论 {thesis_id} 还在 {thesis.status} 阶段，不能记成交："
                f"先走到建仓再记，否则时间线上会出现一个从未发生的「我当时买了」"
            )

    def accounts_of(self, username: str) -> list[Account]:
        """用户名解析成 id 再问仓库：库里那一列存的是 id，屏幕上打的是用户名。"""
        return self.accounts.list_for(resolve_actor(self.users, username).id)

    def position(self, account_id: str, market: str, code: str) -> Position | None:
        """没有持仓就是没有，不是报错：屏上「空仓」与「这只票不存在」是两回事。"""
        return self.orders.position(account_id, market, code, strict=False)

    def cash(self, account_id: str) -> dict[str, float]:
        return self.accounts.cash_balances(account_id)
