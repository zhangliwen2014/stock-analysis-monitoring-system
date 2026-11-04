"""组合限额的红线（S4-E §2）。

一行 = 一条用户自己定的红线；`account_id` 为空表示全部账户。同一个
`(用户, 账户, 种类, 对象)` 只留一行：再存一次是改阈值，不是多出一条会互相打架的线。

阈值一律是小数比值（0.2 = 20%）：屏幕上的百分号是显示层的事，
库里存两种标度，读的人总有一天会拿 20 当 20% 用。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.storage.db import Database
from sams.storage.ids import new_id

_COLUMNS = "id, user_id, account_id, kind, scope, threshold, note, created_at"


@dataclass(frozen=True)
class PortfolioLimit:
    id: str
    user_id: str
    account_id: str | None
    kind: str
    #: 空串 = 没有对象（只有回撤线是这样）
    scope: str
    threshold: float
    note: str | None
    created_at: datetime


def _row(row: dict) -> PortfolioLimit:
    return PortfolioLimit(
        id=str(row["id"]),
        user_id=str(row["user_id"]),
        account_id=None if row["account_id"] is None else str(row["account_id"]),
        kind=str(row["kind"]),
        scope=str(row["scope"] or ""),
        threshold=float(row["threshold"]),
        note=row["note"],
        created_at=row["created_at"],
    )


class LimitRepo:
    def __init__(self, db: Database, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.db = db
        self.clock = clock

    def list_for(
        self, user_id: str, *, account_id: str | None = None
    ) -> tuple[PortfolioLimit, ...]:
        """不给账户就是这个人全部的红线；给了账户则含「全部账户」那几条。"""
        if account_id is None:
            rows = self.db.query(
                f"SELECT {_COLUMNS} FROM portfolio_limits WHERE user_id = ?"
                " ORDER BY kind, scope, created_at",
                [user_id],
            )
        else:
            rows = self.db.query(
                f"SELECT {_COLUMNS} FROM portfolio_limits WHERE user_id = ?"
                " AND (account_id IS NULL OR account_id = ?)"
                " ORDER BY kind, scope, created_at",
                [user_id, account_id],
            )
        return tuple(_row(row) for row in rows.to_dicts())

    def find(
        self, user_id: str, *, account_id: str | None, kind: str, scope: str
    ) -> PortfolioLimit | None:
        """按「同一屏上的同一条线」找。

        账户为空与账户相同是两回事：`IS NOT DISTINCT FROM` 才分得清。
        """
        rows = self.db.query(
            f"SELECT {_COLUMNS} FROM portfolio_limits WHERE user_id = ?"
            " AND account_id IS NOT DISTINCT FROM ? AND kind = ? AND scope = ?",
            [user_id, account_id, kind, scope],
        )
        return None if rows.is_empty() else _row(rows.to_dicts()[0])

    def save(
        self,
        user_id: str,
        *,
        account_id: str | None,
        kind: str,
        scope: str,
        threshold: float,
        note: str | None = None,
    ) -> PortfolioLimit:
        """同一条线再存一次是改阈值：两条同对象的线各说各话，判违规时听谁的都错。"""
        existing = self.find(user_id, account_id=account_id, kind=kind, scope=scope)
        if existing is not None:
            self.db.execute(
                "UPDATE portfolio_limits SET threshold = ?, note = ? WHERE id = ?"
                " AND user_id = ?",
                [threshold, note, existing.id, user_id],
            )
            return PortfolioLimit(
                id=existing.id,
                user_id=existing.user_id,
                account_id=existing.account_id,
                kind=existing.kind,
                scope=existing.scope,
                threshold=threshold,
                note=note,
                created_at=existing.created_at,
            )
        saved = PortfolioLimit(
            id=new_id(),
            user_id=user_id,
            account_id=account_id,
            kind=kind,
            scope=scope,
            threshold=threshold,
            note=note,
            created_at=self.clock.now_utc(),
        )
        self.db.execute(
            f"INSERT INTO portfolio_limits ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                saved.id,
                saved.user_id,
                saved.account_id,
                saved.kind,
                saved.scope,
                saved.threshold,
                saved.note,
                saved.created_at,
            ],
        )
        return saved

    def get(self, user_id: str, limit_id: str) -> PortfolioLimit | None:
        rows = self.db.query(
            f"SELECT {_COLUMNS} FROM portfolio_limits WHERE user_id = ? AND id = ?",
            [user_id, limit_id],
        )
        return None if rows.is_empty() else _row(rows.to_dicts()[0])

    def delete(self, user_id: str, limit_id: str) -> bool:
        """归属写进 WHERE：别人的 id 删不动，也看不出它存不存在。"""
        if self.get(user_id, limit_id) is None:
            return False
        self.db.execute(
            "DELETE FROM portfolio_limits WHERE user_id = ? AND id = ?", [user_id, limit_id]
        )
        return True
