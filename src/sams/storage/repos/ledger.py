"""模拟盘账本的仓库层（spec §5.4）。

四张表的写入口收在这一个模块里：`SimAccountRepo`（账户/现金/流水）与 `SimOrderRepo`
（订单/持仓）。放在同一层是因为一次成交天然跨两张表，而分开放的话
「谁负责开事务」会变成口头约定。

非负现金、币种一致这类前置条件在仓库强制，理由同 D81：M3 会引入绕过服务层的写入口，
一个忘了查的入口留下的不是一句报错，而是一本看起来正常的假账。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite

import polars as pl

from sams.core import funds
from sams.core.codes import canonical_code, canonical_market
from sams.core.config import MarketCfg
from sams.core.errors import StorageError, ValidationError
from sams.storage.db import Database
from sams.storage.ids import new_id
from sams.storage.repos.symbols import SymbolRepo

#: 认得的币种：三字母代码，不做模糊匹配。`RMB`/`rmb` 一律拒绝——
#: 让它过的话，同一个口袋会在库里以两种写法各存一行，余额从此对不上。
_CURRENCIES = frozenset(
    {"CNY", "USD", "HKD", "EUR", "JPY", "GBP", "SGD", "AUD", "CAD"}
)


def _moment(ts: datetime | None) -> datetime:
    """成交/调现金的时刻：带时区才收，裸 datetime 一律拒。

    TIMESTAMPTZ 列会按会话时区解释无时区的值，那等于让 A 股的一笔记成 UTC，差 8 小时，
    而屏幕上看不出来。
    """
    if ts is None:
        return datetime.now(UTC)
    if ts.tzinfo is None:
        raise ValidationError(
            "时间必须带时区（例如 2024-10-25T15:00+08:00）：裸时刻会被当成 UTC"
        )
    return ts.astimezone(UTC)


@dataclass(frozen=True)
class Account:
    id: str
    user_id: str
    name: str
    broker: str | None
    market_scope: tuple[str, ...]
    fee_model: str
    margin_enabled: bool
    report_currency: str


def _row_to_account(row: dict) -> Account:
    return Account(
        id=row["id"],
        user_id=row["user_id"],
        name=row["name"],
        broker=row["broker"],
        market_scope=tuple(json.loads(row["market_scope"] or "[]")),
        fee_model=row["fee_model"],
        margin_enabled=bool(row["margin_enabled"]),
        report_currency=row["report_currency"],
    )


@dataclass(frozen=True)
class FxTrade:
    """账户内一次显式换汇：多币种现金池与券商对账靠它（§5.4 规则 4、D207）。"""

    id: str
    account_id: str
    from_ccy: str
    to_ccy: str
    from_amt: float
    to_amt: float
    rate: float
    ts: datetime


class SimAccountRepo:
    """账户、按币种的现金余额、以及每一分钱的来处。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    def open(
        self,
        user_id: str,
        name: str,
        *,
        broker: str | None = None,
        market_scope: tuple[str, ...] = (),
        fee_model: str = "default",
        margin_enabled: bool = False,
        report_currency: str = "CNY",
    ) -> Account:
        if not name.strip():
            raise ValidationError("账户名不能为空")
        account_id = new_id()
        try:
            self.db.execute(
                "INSERT INTO sim_accounts (id, user_id, name, broker, market_scope, "
                "fee_model, margin_enabled, report_currency) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    account_id,
                    user_id,
                    name,
                    broker,
                    json.dumps(list(market_scope)),
                    fee_model,
                    margin_enabled,
                    report_currency,
                ],
            )
        except StorageError as exc:
            # 唯一键 (user_id, name) 冲突要读成「这个名字已经有了」，不是「SQL 失败」
            if "Constraint" in str(exc) or "unique" in str(exc).lower():
                raise StorageError(f"账户 {name!r} 已存在（每个用户的账户名唯一）") from exc
            raise
        account = self.get(user_id, account_id)
        assert account is not None  # 刚写完，读不到就是库出了问题
        return account

    def rename(self, user_id: str, account_id: str, name: str) -> Account:
        """改名只覆盖 `name`：持仓、成交、现金与规则都挂 id，不跟着名字走（D204）。"""
        clean = name.strip()
        if not clean:
            raise ValidationError("账户名不能为空")
        if self.get(user_id, account_id) is None:
            raise StorageError(f"账户 {account_id} 不存在")
        try:
            self.db.execute(
                "UPDATE sim_accounts SET name = ? WHERE id = ? AND user_id = ?",
                [clean, account_id, user_id],
            )
        except StorageError as exc:
            # 与 `open` 同一句：重名是「这一步不让走」，不是「库坏了」
            if "Constraint" in str(exc) or "unique" in str(exc).lower():
                raise StorageError(f"账户 {clean!r} 已存在（每个用户的账户名唯一）") from exc
            raise
        account = self.get(user_id, account_id)
        assert account is not None  # 刚改完，读不到就是库出了问题
        return account

    def get(self, user_id: str, account_id: str) -> Account | None:
        rows = self.db.query(
            "SELECT * FROM sim_accounts WHERE id = ? AND user_id = ?",
            [account_id, user_id],
        )
        return None if rows.is_empty() else _row_to_account(rows.to_dicts()[0])

    def list_for(self, user_id: str) -> list[Account]:
        rows = self.db.query(
            "SELECT * FROM sim_accounts WHERE user_id = ? ORDER BY created_at",
            [user_id],
        )
        return [_row_to_account(r) for r in rows.to_dicts()]

    def count(self) -> int:
        """全库账户数（跨用户）：健康页要的是「这套系统有没有人在记账」，不是某人名下几个。

        归属在这里没有意义——按用户数就得先有一个用户参数，而健康检查恰好不知道是谁。
        """
        return int(self.db.scalar("SELECT count(*) FROM sim_accounts") or 0)

    def cash_balances(self, account_id: str) -> dict[str, float]:
        rows = self.db.query(
            "SELECT currency, amount FROM sim_cash_balances WHERE account_id = ? "
            "ORDER BY currency",
            [account_id],
        )
        return {r["currency"]: float(r["amount"]) for r in rows.to_dicts()}

    def cash_history(self, account_id: str) -> pl.DataFrame:
        """审计行按时间正序：倒序的流水在核对「哪一步开始对不上」时更难读。"""
        return self.db.query(
            "SELECT currency, delta, reason, actor_id, ts FROM sim_cash_moves "
            "WHERE account_id = ? ORDER BY ts, id",
            [account_id],
        )

    def adjust_cash(
        self,
        account_id: str,
        currency: str,
        delta: float,
        *,
        reason: str,
        actor_id: str,
        ts: datetime | None = None,
    ) -> float:
        """动现金的唯一入口：余额与流水同事务，失败整段回滚。

        返回调整后的余额，因为调用方（成交记账）要拿它判断「这一步之后还有多少钱」。
        """
        if not reason or not reason.strip():
            raise ValidationError("调整现金必须给理由：审计行没有理由就不是审计")
        if delta == 0:
            raise ValidationError("delta 不能是 0：没动就不要留一条流水")
        if currency not in _CURRENCIES:
            raise ValidationError(f"不认识币种 {currency!r}：三字母代码，不做模糊匹配")
        account = self._require(account_id)
        moment = _moment(ts)
        current = float(
            self.db.scalar(
                "SELECT amount FROM sim_cash_balances WHERE account_id = ? AND currency = ?",
                [account_id, currency],
            )
            or 0.0
        )
        after = current + delta
        if after < 0 and not account.margin_enabled:
            raise StorageError(
                f"现金不足：{currency} 余额 {current:.2f}，本次要动 {delta:.2f}，"
                f"结果 {after:.2f}。无杠杆账户不透支（§5.4）；"
                f"要记融资请在开户时把 margin_enabled 打开，或换一笔够钱的金额"
            )
        with self.db.transaction():
            self.db.execute(
                "INSERT INTO sim_cash_moves"
                " (id, account_id, currency, delta, reason, actor_id, ts)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                [new_id(), account_id, currency, delta, reason, actor_id, moment],
            )
            self.db.execute(
                "INSERT OR REPLACE INTO sim_cash_balances"
                " (account_id, currency, amount, updated_at) VALUES (?, ?, ?, ?)",
                [account_id, currency, after, moment],
            )
        return after

    def convert(
        self,
        account_id: str,
        from_ccy: str,
        to_ccy: str,
        amount: float,
        rate: float,
        *,
        actor_id: str,
        ts: datetime | None = None,
    ) -> FxTrade:
        """显式换汇：换汇记录与两条现金流水同进同退（§5.4 规则 4、D207）。

        不做自动折算：汇率是这笔换汇的成交价，由用户按券商的实际价填进来；
        库里那条只用来预填表单。
        """
        from_ccy = (from_ccy or "").strip().upper()
        to_ccy = (to_ccy or "").strip().upper()
        if from_ccy == to_ccy:
            raise ValidationError("换汇的两个币种要不同：同币种恒为 1，不需要换")
        for currency in (from_ccy, to_ccy):
            if currency not in _CURRENCIES:
                raise ValidationError(f"不认识币种 {currency!r}：三字母代码，不做模糊匹配")
        if not isfinite(amount) or amount <= 0:
            raise ValidationError(f"换汇金额必须是正有限小数，收到 {amount!r}")
        if not isfinite(rate) or rate <= 0:
            raise ValidationError(f"汇率必须是正有限小数，收到 {rate!r}")
        self._require(account_id)
        moment = _moment(ts)
        to_amt = amount * rate
        trade = FxTrade(
            id=new_id(),
            account_id=account_id,
            from_ccy=from_ccy,
            to_ccy=to_ccy,
            from_amt=amount,
            to_amt=to_amt,
            rate=rate,
            ts=moment,
        )
        reason = f"换汇 {from_ccy}→{to_ccy} @{rate:.4f}"
        with self.db.transaction():
            self.db.execute(
                "INSERT INTO fx_trades"
                " (id, account_id, from_ccy, to_ccy, from_amt, to_amt, rate, ts)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    trade.id, account_id, from_ccy, to_ccy,
                    amount, to_amt, rate, moment,
                ],
            )
            self.adjust_cash(
                account_id, from_ccy, -amount, reason=reason, actor_id=actor_id, ts=moment
            )
            self.adjust_cash(
                account_id, to_ccy, to_amt, reason=reason, actor_id=actor_id, ts=moment
            )
        return trade

    def _require(self, account_id: str) -> Account:
        """账户是否存在（不校验归属，那是读用例那一层带 user_id 的事）。"""
        rows = self.db.query("SELECT * FROM sim_accounts WHERE id = ?", [account_id])
        if rows.is_empty():
            raise StorageError(f"账户 {account_id} 不存在")
        return _row_to_account(rows.to_dicts()[0])


BUY = "buy"
SELL = "sell"
SIDES = (BUY, SELL)

#: 一次成交动的四张表里，只有订单是「历史」；持仓和余额是「状态」。
#: 状态可以由历史重放出来，所以写状态必须在历史之后、同一个事务里。


@dataclass(frozen=True)
class Position:
    account_id: str
    market: str
    code: str
    currency: str
    qty: float
    avg_cost: float
    #: 第一次建仓那一刻（不是最后一次成交）：§9.3 的「拿了多久」全靠它
    open_ts: datetime | None
    last_price: float | None
    last_price_ts: datetime | None


@dataclass(frozen=True)
class Order:
    id: str
    account_id: str
    market: str
    code: str
    currency: str
    side: str
    qty: float
    price: float
    ts: datetime
    fee: float
    thesis_id: str | None
    fx_rate_ref: float | None


def _row_to_order(row: dict) -> Order:
    return Order(
        id=row["id"],
        account_id=row["account_id"],
        market=row["market"],
        code=row["code"],
        currency=row["currency"],
        side=row["side"],
        qty=float(row["qty"]),
        price=float(row["price"]),
        ts=row["ts"],
        fee=float(row["fee"] or 0.0),
        thesis_id=row["thesis_id"],
        fx_rate_ref=row.get("fx_rate_ref"),
    )


def _row_to_position(row: dict) -> Position:
    return Position(
        account_id=row["account_id"],
        market=row["market"],
        code=row["code"],
        currency=row["currency"],
        qty=float(row["qty"]),
        avg_cost=float(row["avg_cost"]),
        open_ts=row["open_ts"],
        last_price=row["last_price"],
        last_price_ts=row["last_price_ts"],
    )


class SimOrderRepo:
    """订单与持仓。`record_fill` 是唯一的成交入口。"""

    def __init__(
        self,
        db: Database,
        accounts: SimAccountRepo | None = None,
        *,
        markets: dict[str, MarketCfg] | None = None,
        symbols: SymbolRepo | None = None,
    ) -> None:
        self.db = db
        #: 现金与流水归 SimAccountRepo 管；成交要顺手动它，所以持有同一个 db 上的一个实例
        self.accounts = accounts or SimAccountRepo(db)
        #: 两个都不传就没有币种闸门（闸门是加的，不是既有的：见 test_no_markets_...）
        self.markets = markets
        self.symbols = symbols

    def record_fill(
        self,
        *,
        account_id: str,
        user_id: str,
        market: str,
        code: str,
        side: str,
        qty: float,
        price: float,
        currency: str,
        fee: float = 0.0,
        thesis_id: str | None = None,
        fx_rate_ref: float | None = None,
        ts: datetime | None = None,
    ) -> Order:
        if side not in SIDES:
            raise ValidationError(f"side 只能是 {'/'.join(SIDES)}，收到 {side!r}")
        for label, value in (("qty", qty), ("price", price), ("fee", fee)):
            if not isfinite(value):
                raise ValidationError(f"{label} 必须是有限小数，收到 {value!r}")
        if qty <= 0:
            raise ValidationError("qty 必须大于 0：清仓请用 sell 而不是 buy 0")
        if price <= 0:
            raise ValidationError(
                "价格 必须大于 0：负价格是一笔「买它还倒找钱」的买入——现金反而变多，"
                "而成本基础会落到零以下，此后这一行的每一笔 price_pnl 都看着正常而其实是错数。"
                "要记退款或调整，那是现金动作（sams ledger cash），不是一笔成交"
            )
        if fee < 0:
            raise ValidationError("手续费不能为负：负手续费等于把成本做低")
        market = canonical_market(market)
        raw_code = code
        code = canonical_code(market, code)
        self._check_currency(market, code, currency)
        self._check_market_of_code(market, raw_code, code)
        moment = _moment(ts)
        existing = self.position(account_id, market, code, strict=False)
        held = existing.qty if existing else 0.0
        if side == SELL and qty > held:
            raise StorageError(f"超卖：{market} {code} 持有 {held:g}，本次要卖 {qty:g}")
        gross = qty * price
        cash_delta = -gross - fee if side == BUY else gross - fee
        if side == BUY:
            balances = self.accounts.cash_balances(account_id)
            if currency not in balances:
                raise StorageError(
                    f"账户没有 {currency} 现金，买不了 {currency} 定价的 {market} {code}"
                    f"（现有：{', '.join(sorted(balances)) or '空'}）。"
                    f"不自动换汇：先在持仓页「换汇」把 {currency} 换出来"
                    f"（填实际成交汇率），或记一笔同币种现金并写明理由（§5.4 规则 4）"
                )
        order_id = new_id()
        with self.db.transaction():
            # `fx_rate_applied` 留 NULL：那一列按 spec §5.2 只在「这笔伴随换汇」时才有值，
            # 换汇本身要到 M5 的 fx_trades 才存在。把基准汇率抄进它会让它从此没有出处。
            self.db.execute(
                "INSERT INTO sim_orders (id, account_id, market, code, currency, side, qty,"
                " price, ts, fee, fee_currency, thesis_id, fx_rate_ref)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    order_id, account_id, market, code, currency, side, qty, price,
                    moment, fee, currency, thesis_id, fx_rate_ref,
                ],
            )
            if side == BUY:
                new_qty = held + qty
                new_avg = funds.blended_cost(
                    held, existing.avg_cost if existing else 0.0, qty, price, fee
                )
            else:
                new_qty = held - qty
                # 清仓均价归 0：留着最后一笔成本会被读成「还持有一些」
                new_avg = existing.avg_cost if new_qty > 0 else 0.0
            self.db.execute(
                "INSERT OR REPLACE INTO sim_positions"
                " (account_id, market, code, currency, qty, avg_cost, open_ts, last_price,"
                " last_price_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    account_id, market, code, currency, new_qty, new_avg,
                    # 建仓日是第一次那一刻，不是最后一次：覆盖它等于抹掉「拿了多久」
                    existing.open_ts if existing else moment,
                    price,
                    moment,
                ],
            )
            self.accounts.adjust_cash(
                account_id,
                currency,
                cash_delta,
                reason=f"{'买入' if side == BUY else '卖出'} {code}",
                actor_id=user_id,
                ts=moment,
            )
        order = self.order(account_id, order_id)
        assert order is not None
        return order

    def undo_fill(
        self,
        account_id: str,
        order_id: str,
        *,
        actor_id: str,
        ts: datetime | None = None,
    ) -> bool:
        """撤销一笔成交：现金逐分反做、持仓从其余成交重放、订单行删除（D205）。

        重放而不是「反着乘一遍」：卖出的成本基础取决于它前面那些买入的顺序，
        倒着算会在加仓过的持仓上差出一个数。状态由历史推出来，就永远和历史对得上。
        """
        order = self.order(account_id, order_id)
        if order is None:
            raise StorageError(f"成交 {order_id} 不存在或不属于账户 {account_id}")
        existing = self.position(account_id, order.market, order.code, strict=False)
        remaining = [
            row
            for row in self.orders(account_id)
            if not (row.market == order.market and row.code == order.code and row.id == order.id)
        ]
        running = 0.0
        basis = 0.0
        opened: datetime | None = None
        for row in remaining:
            if row.side == BUY:
                basis += row.qty * row.price + row.fee
                running += row.qty
                if opened is None:
                    opened = row.ts
            else:
                if row.qty > running:
                    raise StorageError(
                        f"这笔买入撤销后，{row.ts:%Y-%m-%d} 的卖出 {row.qty:g} 股会超卖"
                        f"（只剩 {running:g} 股）：先撤销后面那笔卖出"
                    )
                avg = basis / running if running > 0 else 0.0
                basis -= row.qty * avg
                running -= row.qty
        gross = order.qty * order.price
        delta = gross + order.fee if order.side == BUY else -(gross - order.fee)
        moment = _moment(ts)
        with self.db.transaction():
            self.db.execute("DELETE FROM sim_orders WHERE id = ?", [order_id])
            self.db.execute(
                "INSERT OR REPLACE INTO sim_positions"
                " (account_id, market, code, currency, qty, avg_cost, open_ts, last_price,"
                " last_price_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    account_id, order.market, order.code, order.currency,
                    running, (basis / running) if running > 0 else 0.0,
                    opened if running > 0 else None,
                    existing.last_price if existing else None,
                    existing.last_price_ts if existing else None,
                ],
            )
            self.accounts.adjust_cash(
                account_id,
                order.currency,
                delta,
                reason=f"撤销成交 {order.id}",
                actor_id=actor_id,
                ts=moment,
            )
        return True

    def _check_market_of_code(self, market: str, raw_code: str, code: str) -> None:
        """一只票只该在它自己的市场里记账：A 的表单填 HK 00700 要当场拒（D206）。

        只拒「另一个已配置市场有这只票」：两边主档都没有时照收——新上市、退市整理、
        主档还没同步都长这样，把数据缺口变成记账障碍是另一类错。
        """
        if self.markets is None or self.symbols is None:
            return
        if self.symbols.get(market, code) is not None:
            return
        for other in self.markets:
            if other == market:
                continue
            try:
                candidate = canonical_code(other, raw_code)
            except ValidationError:
                continue
            if self.symbols.get(other, candidate) is not None:
                raise StorageError(
                    f"{market} 里没有 {code}，但 {other} 有 {candidate}："
                    f"这笔看起来记错了市场（同一只票要在它自己的市场里记）"
                )

    def _check_currency(self, market: str, code: str, currency: str) -> None:
        """币种三方对齐：配置推定值、库里采集值、这笔单要用的值。不一致就点名两个币种拒掉。

        三条会分叉的路（D31 + §5.4 规则 1）：市场默认币种、`currency_override` 推定的
        标的币种、采集时写进 `symbols.currency` 的那个。分叉时替用户挑一个，等于让记账
        和展示各信一个，屏幕上同一只票两个价格，而没人知道哪一次是错的。
        """
        if self.markets is None:
            return
        expected = funds.quote_currency(market, code, self.markets)
        if currency != expected:
            raise ValidationError(
                f"{market} {code} 的标的币种是 {expected}，这笔单记成了 {currency}。"
                f"录入与存储永远用标的币种（§5.4）；要换币种请先动现金，别动这笔单"
            )
        if self.symbols is None:
            return
        row = self.symbols.get(market, code)
        stored = None if row is None else row.get("currency")
        if stored is not None and stored != expected:
            raise StorageError(
                f"{market} {code} 库里记的币种是 {stored}，与配置推定的 {expected} 不符。"
                f"两边都不能信：请先同步这只票的标的信息，或核对代码是不是另一只票"
                f"（B 股与港股人民币双柜台见 D31）"
            )

    def order(self, account_id: str, order_id: str) -> Order | None:
        rows = self.db.query(
            "SELECT * FROM sim_orders WHERE id = ? AND account_id = ?",
            [order_id, account_id],
        )
        return None if rows.is_empty() else _row_to_order(rows.to_dicts()[0])

    def orders(self, account_id: str) -> list[Order]:
        rows = self.db.query(
            "SELECT * FROM sim_orders WHERE account_id = ? ORDER BY ts, id",
            [account_id],
        )
        return [_row_to_order(r) for r in rows.to_dicts()]

    def order_rows(self, account_id: str) -> int:
        return int(
            self.db.scalar(
                "SELECT count(*) FROM sim_orders WHERE account_id = ?", [account_id]
            )
            or 0
        )

    def orders_for_thesis(self, thesis_id: str) -> list[Order]:
        """U26：某个立论到底有没有真成交。跨账户查，因为 thesis 与账户都属于用户。"""
        rows = self.db.query(
            "SELECT * FROM sim_orders WHERE thesis_id = ? ORDER BY ts, id",
            [thesis_id],
        )
        return [_row_to_order(r) for r in rows.to_dicts()]

    def positions(self, account_id: str) -> list[Position]:
        rows = self.db.query(
            "SELECT * FROM sim_positions WHERE account_id = ? ORDER BY market, code",
            [account_id],
        )
        return [_row_to_position(r) for r in rows.to_dicts()]

    def count_orders(self) -> int:
        """全库成交笔数（跨账户、跨用户）：健康页问的是「这套账本动过没有」。"""
        return int(self.db.scalar("SELECT count(*) FROM sim_orders") or 0)

    def count_holdings(self) -> int:
        """还持有的只数，**不含清仓后留下的 0 股行**。

        `record_fill` 把那一行留在库里当历史（均价归 0），按行数算会把「全都卖了」
        读成「还持有 N 只」——那是这一屏上最容易看错的一格。
        """
        return int(
            self.db.scalar("SELECT count(*) FROM sim_positions WHERE qty <> 0") or 0
        )

    def position(
        self, account_id: str, market: str, code: str, *, strict: bool = True
    ) -> Position | None:
        rows = self.db.query(
            "SELECT * FROM sim_positions WHERE account_id = ? AND market = ? AND code = ?",
            [account_id, canonical_market(market), canonical_code(market, code)],
        )
        if rows.is_empty():
            if strict:
                raise StorageError(f"账户 {account_id} 没有 {market} {code} 的持仓")
            return None
        return _row_to_position(rows.to_dicts()[0])

    def realized_pnl(self, account_id: str, market: str, code: str) -> float:
        """已实现盈亏（标的币种，已扣费）：由订单历史算，不另存一列。

        存下来就得再维护「成本基础随加仓变化」这件事，而它已经在 `sim_orders` 里了。
        """
        rows = self.db.query(
            "SELECT side, qty, price, fee FROM sim_orders WHERE account_id = ?"
            " AND market = ? AND code = ? ORDER BY ts, id",
            [account_id, canonical_market(market), canonical_code(market, code)],
        )
        if rows.is_empty():
            return 0.0
        running = 0.0
        basis = 0.0
        total = 0.0
        for r in rows.to_dicts():
            qty, price, fee = float(r["qty"]), float(r["price"]), float(r["fee"] or 0.0)
            if r["side"] == BUY:
                basis += qty * price + fee
                running += qty
            else:
                avg = basis / running if running > 0 else 0.0
                total += funds.realized_pnl(qty=qty, price=price, avg_cost=avg, fee=fee)
                basis -= qty * avg
                running -= qty
        return total
