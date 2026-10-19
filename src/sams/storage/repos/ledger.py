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
        if fee < 0:
            raise ValidationError("手续费不能为负：负手续费等于把成本做低")
        market = canonical_market(market)
        code = canonical_code(market, code)
        self._check_currency(market, code, currency)
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
                    f"M3 不自动换汇：先补一笔同币种现金并写明理由（§5.4 规则 4）"
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
