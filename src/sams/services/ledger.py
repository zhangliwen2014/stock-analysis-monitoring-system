"""账本的写用例（spec §5.4）。

三件事只在这一层做：用户名解析成库里的 id（`services/actors.py`，与 thesis 认的是同一个 id，
否则 `--thesis` 那条链两边各认各的）、这笔账是不是你的（仓库层的 `_require` 不查归属）、
以及成交能不能挂在一条还没建仓的立论上（问的是状态机，它在 `repos/theses.py` 的
`TRANSITIONS` 里，D81）。币种在这里按配置推定、落库前由仓库校验，调用方不参与。

不 publish 事件：M3-A 没有消费者（M3-C 的规则引擎才会订 `QuoteUpdated`），先挂一个
没人听的 `publish` 会让「这个事件有没有人吃」重新变成要靠读代码猜的事。
"""

from __future__ import annotations

from datetime import datetime

from sams.core import funds
from sams.core.codes import canonical_code, canonical_market
from sams.core.config import Settings
from sams.core.errors import StorageError, ValidationError
from sams.services.actors import resolve_actor
from sams.storage.repos.ledger import Account, Order, Position, SimAccountRepo, SimOrderRepo
from sams.storage.repos.theses import OBSERVING, PROPOSED, ThesisRepo
from sams.storage.repos.users import User, UserRepo

#: 能挂成交的立论状态：还没建仓的不能挂——那会在时间线上留下一个从未发生的「我当时买了」。
#: 归档/失效的那两条能挂：「事后回头看，这笔是按当时的立论买的」是真实发生过的事。
_UNBORN = (OBSERVING, PROPOSED)


class LedgerService:
    def __init__(
        self,
        accounts: SimAccountRepo,
        orders: SimOrderRepo,
        theses: ThesisRepo,
        users: UserRepo,
        settings: Settings,
    ) -> None:
        self.accounts = accounts
        self.orders = orders
        self.theses = theses
        self.users = users
        self.settings = settings

    def open_account(
        self,
        username: str,
        name: str,
        *,
        broker: str | None = None,
        market_scope: tuple[str, ...] = (),
        margin_enabled: bool = False,
    ) -> Account:
        user = resolve_actor(self.users, username)
        return self.accounts.open(
            user.id,
            name,
            broker=broker,
            market_scope=market_scope,
            margin_enabled=margin_enabled,
            report_currency=self.settings.report_currency,
        )

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
        return self.accounts.adjust_cash(
            account_id, currency, abs(amount), reason=reason, actor_id=actor_id
        )

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
        return self.accounts.adjust_cash(
            account_id, currency, -abs(amount), reason=reason, actor_id=actor_id
        )

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
        fx_rate_ref: float | None = None,
        ts: datetime | None = None,
    ) -> Order:
        return self._fill(
            account_id, username, market, code, side="buy", qty=qty, price=price, fee=fee,
            thesis_id=thesis_id, fx_rate_ref=fx_rate_ref, ts=ts,
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
        fx_rate_ref: float | None = None,
        ts: datetime | None = None,
    ) -> Order:
        return self._fill(
            account_id, username, market, code, side="sell", qty=qty, price=price, fee=fee,
            thesis_id=thesis_id, fx_rate_ref=fx_rate_ref, ts=ts,
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
        fx_rate_ref: float | None,
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
        return self.orders.record_fill(
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
            fx_rate_ref=fx_rate_ref,
            ts=ts,
        )

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
