"""自选股仓库（用户域）。

隔离设计：除显式的 list_all（admin 只读审计用）之外，所有读方法都必须带 user_id，
不存在「查全量再由上层过滤」的接口形态——隔离在仓库层兜底，不依赖调用方自觉。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sams.core.errors import ValidationError
from sams.storage.db import Database

MAX_ITEMS_PER_USER = 500


@dataclass(frozen=True)
class WatchItem:
    user_id: str
    market: str
    code: str
    note: str | None = None
    added_at: datetime | None = None


def _require_user(user_id: str) -> str:
    if not user_id:
        raise ValidationError("user_id 不能为空")
    return user_id


class WatchlistRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def add(self, user_id: str, market: str, code: str, *, note: str | None = None) -> None:
        user_id = _require_user(user_id)
        if not market or not code:
            raise ValidationError("market 与 code 不能为空")
        if self.count(user_id) >= MAX_ITEMS_PER_USER and not self.contains(user_id, market, code):
            raise ValidationError(f"自选股数量上限 {MAX_ITEMS_PER_USER}")
        self.db.execute(
            "INSERT OR REPLACE INTO watchlists (user_id, market, code, note) VALUES (?, ?, ?, ?)",
            [user_id, market, code, note],
        )

    def remove(self, user_id: str, market: str, code: str) -> bool:
        before = self.count(user_id)
        self.db.execute(
            "DELETE FROM watchlists WHERE user_id = ? AND market = ? AND code = ?",
            [_require_user(user_id), market, code],
        )
        return self.count(user_id) < before

    def contains(self, user_id: str, market: str, code: str) -> bool:
        return (
            self.db.scalar(
                "SELECT count(*) FROM watchlists WHERE user_id = ? AND market = ? AND code = ?",
                [_require_user(user_id), market, code],
            )
            or 0
        ) > 0

    def list(self, user_id: str) -> list[WatchItem]:
        rows = self.db.query(
            "SELECT user_id, market, code, note, added_at FROM watchlists "
            "WHERE user_id = ? ORDER BY added_at, code",
            [_require_user(user_id)],
        )
        return _to_items(rows)

    def codes(self, user_id: str) -> list[tuple[str, str]]:
        return [(item.market, item.code) for item in self.list(user_id)]

    def count(self, user_id: str) -> int:
        return int(
            self.db.scalar("SELECT count(*) FROM watchlists WHERE user_id = ?", [user_id]) or 0
        )

    def list_all(self) -> list[WatchItem]:
        """admin 审计视图：返回全部用户的自选股。只读，无配套写方法。"""
        rows = self.db.query(
            "SELECT user_id, market, code, note, added_at FROM watchlists "
            "ORDER BY user_id, added_at, code"
        )
        return _to_items(rows)


def _to_items(rows) -> list[WatchItem]:
    return [
        WatchItem(
            user_id=r["user_id"],
            market=r["market"],
            code=r["code"],
            note=r["note"],
            added_at=r["added_at"],
        )
        for r in rows.to_dicts()
    ]
