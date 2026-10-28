"""自选股仓库（用户域）。

隔离设计：除显式的 list_all（admin 只读审计用）之外，所有读方法都必须带 user_id，
不存在「查全量再由上层过滤」的接口形态——隔离在仓库层兜底，不依赖调用方自觉。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sams.core.codes import canonical_code, canonical_market
from sams.core.errors import ValidationError
from sams.storage.db import Database

MAX_ITEMS_PER_USER = 500


def _norm(market: str, code: str) -> tuple[str, str]:
    """清单里的键必须和 bars 同一个形态——市场名与代码两半都是。

    不归一时 `700` 落库成功、永远查不到 00700 的数据，而且不报错——用户看到的是
    「这个自选股没有行情」，实际是键写错了。仓库层兜底，不指望调用方先归一。
    小写市场名同理：`a/601088` 这一行在清单里看得见，行情与告警按 `A` 查，一律读不到，
    而这条链上再没有别人会归一它（D46 的下沉）。
    """
    if not market or not code:
        raise ValidationError("market 与 code 不能为空")
    try:
        cleaned = canonical_market(market)
        return cleaned, canonical_code(cleaned, code)
    except ValueError as exc:
        # CLI 只把 SamsError 当「用户输入错了」，ValueError 会炸出栈回溯
        raise ValidationError(str(exc)) from exc


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
        """重复添加是更新，不是重写整行：没带新批注就留下旧的（D169）。

        「移除」只是折起，之后在搜索框/选股页再点一次加自选很常见；`INSERT OR REPLACE`
        会把 `note` 写成 NULL，把用户写过的内容连同那一行的位置一起抹掉。`added_at`
        由 UPDATE 天然保住，顺序也就保住了。
        """
        user_id = _require_user(user_id)
        market, code = _norm(market, code)
        if self.count(user_id) >= MAX_ITEMS_PER_USER and not self.contains(user_id, market, code):
            raise ValidationError(f"自选股数量上限 {MAX_ITEMS_PER_USER}")
        existing = self.db.query(
            "SELECT note FROM watchlists WHERE user_id = ? AND market = ? AND code = ?",
            [user_id, market, code],
        )
        if existing.height:
            kept = note if note is not None else existing["note"][0]
            self.db.execute(
                "UPDATE watchlists SET note = ?, removed_at = NULL "
                "WHERE user_id = ? AND market = ? AND code = ?",
                [kept, user_id, market, code],
            )
            return
        self.db.execute(
            "INSERT INTO watchlists (user_id, market, code, note, removed_at)"
            " VALUES (?, ?, ?, ?, NULL)",
            [user_id, market, code, note],
        )

    def remove(self, user_id: str, market: str, code: str) -> bool:
        """折起：从看板与规则展开里消失，但仍在 `list_all()`（= 每日采集清单）里（D169）。

        返回值的含义与硬删除时代一致：「这一行原来是可见的」。第二次折同一只票
        返回 False——它本来就折着，没有发生任何变化，界面不该报「刚刚移掉了」。
        """
        market, code = _norm(market, code)
        user_id = _require_user(user_id)
        # `contains()` 只认未折起的行，所以它天然就是「这一行原来看得见吗」，不必先 SELECT 再 UPDATE
        if not self.contains(user_id, market, code):
            return False
        self.db.execute(
            "UPDATE watchlists SET removed_at = current_timestamp "
            "WHERE user_id = ? AND market = ? AND code = ?",
            [user_id, market, code],
        )
        return True

    def hidden(self, user_id: str) -> list[WatchItem]:
        """折起的那几行：只给自选页底部那一栏用，看板与采集都不读它。"""
        rows = self.db.query(
            "SELECT user_id, market, code, note, added_at FROM watchlists "
            "WHERE user_id = ? AND removed_at IS NOT NULL ORDER BY removed_at, code",
            [_require_user(user_id)],
        )
        return _to_items(rows)

    def restore(self, user_id: str, market: str, code: str) -> bool:
        """`removed_at = NULL`。折起来的东西必须能一句话放回去，这是这一条存在的理由。"""
        market, code = _norm(market, code)
        user_id = _require_user(user_id)
        folded = self.db.scalar(
            "SELECT count(*) FROM watchlists "
            "WHERE user_id = ? AND market = ? AND code = ? AND removed_at IS NOT NULL",
            [user_id, market, code],
        )
        if not folded:
            return False
        self.db.execute(
            "UPDATE watchlists SET removed_at = NULL "
            "WHERE user_id = ? AND market = ? AND code = ?",
            [user_id, market, code],
        )
        return True

    def purge(self, user_id: str, market: str, code: str) -> bool:
        """真删那一刀：连 `list_all()` 里的采集条目一起放手，所以屏幕上要先确认一屏（D169）。

        折起的行也删得掉——用户明确要求删的时候，它折不折着都不该拦住这一刀。
        """
        market, code = _norm(market, code)
        user_id = _require_user(user_id)
        exists = self.db.scalar(
            "SELECT count(*) FROM watchlists WHERE user_id = ? AND market = ? AND code = ?",
            [user_id, market, code],
        )
        if not exists:
            return False
        self.db.execute(
            "DELETE FROM watchlists WHERE user_id = ? AND market = ? AND code = ?",
            [user_id, market, code],
        )
        return True

    def contains(self, user_id: str, market: str, code: str) -> bool:
        market, code = _norm(market, code)
        return (
            self.db.scalar(
                "SELECT count(*) FROM watchlists "
                "WHERE user_id = ? AND market = ? AND code = ? AND removed_at IS NULL",
                [_require_user(user_id), market, code],
            )
            or 0
        ) > 0

    def list(self, user_id: str) -> list[WatchItem]:
        rows = self.db.query(
            "SELECT user_id, market, code, note, added_at FROM watchlists "
            "WHERE user_id = ? AND removed_at IS NULL ORDER BY added_at, code",
            [_require_user(user_id)],
        )
        return _to_items(rows)

    def codes(self, user_id: str) -> list[tuple[str, str]]:
        return [(item.market, item.code) for item in self.list(user_id)]

    def count(self, user_id: str) -> int:
        """不滤 `removed_at`：上限数的是「这只票在不在我的清单里」，折起来不腾出配额。"""
        return int(
            self.db.scalar("SELECT count(*) FROM watchlists WHERE user_id = ?", [user_id]) or 0
        )

    def list_all(self) -> list[WatchItem]:
        """admin 审计视图：返回全部用户的自选股。只读，无配套写方法。

        一条 WHERE 都不加，**折起的行也算在内**：这一份同时是每日采集清单的来源
        （`scheduler/universe.py`），折起那天起停更就等于把恢复之后的历史挖断了（D169）。
        """
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
