"""选股方案的当前名单（S4-B / D255）：进名单记 `first_seen`，留在名单更新 `last_seen`。

这里只保存「现在是谁」：一只票离开后行就被删掉，再来时是一次新的命中。保留历史会让
「新命中」变成「上次也来过」，而用户要的正是「今天新进来的是谁」。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from sams.storage.db import Database


@dataclass(frozen=True)
class HitSync:
    #: 本次新进名单的代码（排序后，便于页面与通知按同一顺序念）
    new: tuple[str, ...]
    kept: int
    removed: int


class ScreenerHitRepo:
    TABLE = "screener_hits"

    def __init__(self, db: Database) -> None:
        self.db = db

    def sync(
        self,
        user_id: str,
        preset_id: str,
        market: str,
        codes: Iterable[str],
        day: date,
    ) -> HitSync:
        """把这一方案的名单对齐到 `codes`；返回进出名单的那两批。"""
        wanted = set(codes)
        with self.db.transaction():
            rows = self.db.query(
                f"SELECT code FROM {self.TABLE} WHERE user_id = ? AND preset_id = ?",
                [user_id, preset_id],
            ).to_dicts()
            existing = {str(row["code"]) for row in rows}
            new = sorted(wanted - existing)
            removed = sorted(existing - wanted)
            kept = sorted(existing & wanted)
            for code in new:
                self.db.execute(
                    f"INSERT INTO {self.TABLE}"
                    " (user_id, preset_id, market, code, first_seen, last_seen)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    [user_id, preset_id, market, code, day, day],
                )
            if kept:
                marks = ",".join("?" for _ in kept)
                self.db.execute(
                    f"UPDATE {self.TABLE} SET market = ?, last_seen = ?"
                    f" WHERE user_id = ? AND preset_id = ? AND code IN ({marks})",
                    [market, day, user_id, preset_id, *kept],
                )
            if removed:
                marks = ",".join("?" for _ in removed)
                self.db.execute(
                    f"DELETE FROM {self.TABLE}"
                    f" WHERE user_id = ? AND preset_id = ? AND code IN ({marks})",
                    [user_id, preset_id, *removed],
                )
        return HitSync(new=tuple(new), kept=len(kept), removed=len(removed))

    def members(self, user_id: str, preset_id: str) -> dict[str, date]:
        rows = self.db.query(
            f"SELECT code, first_seen FROM {self.TABLE}"
            " WHERE user_id = ? AND preset_id = ? ORDER BY code",
            [user_id, preset_id],
        )
        return {str(row["code"]): row["first_seen"] for row in rows.to_dicts()}

    def count(self, user_id: str, preset_id: str) -> int:
        return int(
            self.db.scalar(
                f"SELECT count(*) FROM {self.TABLE} WHERE user_id = ? AND preset_id = ?",
                [user_id, preset_id],
            )
            or 0
        )
