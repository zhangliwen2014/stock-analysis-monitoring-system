"""账本的写用例（spec §5.4）。

币种由配置推定、由仓库校验；thesis 链接由服务层校验，因为「这个立论现在能不能记成交」
问的是状态机，而状态机已经在 `repos/theses.py` 的 `TRANSITIONS` 里（D81）。

不 publish 事件：M3-A 没有消费者（M3-C 的规则引擎才会订 `QuoteUpdated`），先挂一个
没人听的 `publish` 会让「这个事件有没有人吃」重新变成要靠读代码猜的事。
"""

from __future__ import annotations

from datetime import datetime

from sams.core import funds
from sams.core.codes import canonical_code, canonical_market
from sams.core.config import Settings
from sams.core.errors import ValidationError
from sams.storage.repos.ledger import Account, Order, Position, SimAccountRepo, SimOrderRepo
from sams.storage.repos.theses import OBSERVING, PROPOSED, ThesisRepo

#: 能挂成交的立论状态：还没建仓的不能挂——那会在时间线上留下一个从未发生的「我当时买了」。
#: 归档/失效的那两条能挂：「事后回头看，这笔是按当时的立论买的」是真实发生过的事。
_UNBORN = (OBSERVING, PROPOSED)


class LedgerService:
    def __init__(
        self,
        accounts: SimAccountRepo,
        orders: SimOrderRepo,
        theses: ThesisRepo,
        settings: Settings,
    ) -> None:
        self.accounts = accounts
        self.orders = orders
        self.theses = theses
        self.settings = settings

    def open_account(
        self,
        user_id: str,
        name: str,
        *,
        broker: str | None = None,
        market_scope: tuple[str, ...] = (),
        margin_enabled: bool = False,
    ) -> Account:
        return self.accounts.open(
            user_id,
            name,
            broker=broker,
            market_scope=market_scope,
            margin_enabled=margin_enabled,
            report_currency=self.settings.report_currency,
        )

    def deposit(
        self, account_id: str, currency: str, amount: float, *, reason: str, actor_id: str
    ) -> float:
        return self.accounts.adjust_cash(
            account_id, currency, abs(amount), reason=reason, actor_id=actor_id
        )

    def withdraw(
        self, account_id: str, currency: str, amount: float, *, reason: str, actor_id: str
    ) -> float:
        return self.accounts.adjust_cash(
            account_id, currency, -abs(amount), reason=reason, actor_id=actor_id
        )

    def buy(
        self,
        account_id: str,
        user_id: str,
        market: str,
        code: str,
        *,
        qty: float,
        price: float,
        fee: float = 0.0,
        thesis_id: str | None = None,
        fx_rate_ref: float | None = None,
        ts: datetime | None = None,
    ) -> Order:
        return self._fill(
            account_id, user_id, market, code, side="buy", qty=qty, price=price, fee=fee,
            thesis_id=thesis_id, fx_rate_ref=fx_rate_ref, ts=ts,
        )

    def sell(
        self,
        account_id: str,
        user_id: str,
        market: str,
        code: str,
        *,
        qty: float,
        price: float,
        fee: float = 0.0,
        thesis_id: str | None = None,
        fx_rate_ref: float | None = None,
        ts: datetime | None = None,
    ) -> Order:
        return self._fill(
            account_id, user_id, market, code, side="sell", qty=qty, price=price, fee=fee,
            thesis_id=thesis_id, fx_rate_ref=fx_rate_ref, ts=ts,
        )

    def _fill(
        self,
        account_id: str,
        user_id: str,
        market: str,
        code: str,
        *,
        side: str,
        qty: float,
        price: float,
        fee: float,
        thesis_id: str | None,
        fx_rate_ref: float | None,
        ts: datetime | None,
    ) -> Order:
        market = canonical_market(market)
        code = canonical_code(market, code)
        # 未配置的市场在这里就拒：它的币种推不出来（同 D84 的立论闸门），
        # 而且此时还没开事务，报错不会留下半笔账
        currency = funds.quote_currency(market, code, self.settings.markets)
        if thesis_id is not None:
            self._check_thesis(user_id, thesis_id)
        return self.orders.record_fill(
            account_id=account_id,
            user_id=user_id,
            market=market,
            code=code,
            side=side,
            qty=qty,
            price=price,
            currency=currency,
            fee=fee,
            thesis_id=thesis_id,
            fx_rate_ref=fx_rate_ref,
            ts=ts,
        )

    def _check_thesis(self, user_id: str, thesis_id: str) -> None:
        thesis = self.theses.get(user_id, thesis_id)
        if thesis is None:
            # 不存在与不属于同一个回答（`ThesisRepo.get` 按 user_id 查，拿不到内容）
            raise ValidationError(f"立论 {thesis_id} 不存在或不属于用户 {user_id}")
        if thesis.status in _UNBORN:
            raise ValidationError(
                f"立论 {thesis_id} 还在 {thesis.status} 阶段，不能记成交："
                f"先走到建仓再记，否则时间线上会出现一个从未发生的「我当时买了」"
            )

    def accounts_of(self, user_id: str) -> list[Account]:
        return self.accounts.list_for(user_id)

    def position(self, account_id: str, market: str, code: str) -> Position | None:
        """没有持仓就是没有，不是报错：屏上「空仓」与「这只票不存在」是两回事。"""
        return self.orders.position(account_id, market, code, strict=False)

    def cash(self, account_id: str) -> dict[str, float]:
        return self.accounts.cash_balances(account_id)
