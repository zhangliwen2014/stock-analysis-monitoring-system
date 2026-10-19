"""模拟盘账本的仓库层（spec §5.4）。

四张表的写入口收在这一个模块里：`SimAccountRepo`（账户/现金/流水）与 `SimOrderRepo`
（订单/持仓，见 T-4）。放在同一层是因为一次成交天然跨两张表，而分开放的话
「谁负责开事务」会变成口头约定。

非负现金、币种一致这类前置条件在仓库强制，理由同 D81：M3 会引入绕过服务层的写入口，
一个忘了查的入口留下的不是一句报错，而是一本看起来正常的假账。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

import polars as pl

from sams.core.errors import StorageError, ValidationError
from sams.storage.db import Database
from sams.storage.ids import new_id

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
