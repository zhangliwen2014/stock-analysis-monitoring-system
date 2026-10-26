"""`symbol_master` 的读写：全市场代码表，搜索加票的唯一入口。

五档排序（精确代码 > 首字母精确 > 全拼精确 > 前缀 > 片段）在一条 SQL 里定序：
拆成五段查询再 Python 合并，症状是每一档各自对、全局序错乱，而 LIMIT 10 截错边。
搜索永不 LIKE：`contains(lower(x), ?)` 是房规（见 `repos/annotations.py`）。
"""

from __future__ import annotations

from sams.storage.db import Database


class SymbolListRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert_batch(self, market: str, rows: list[dict]) -> None:
        """一行一 INSERT OR REPLACE：整表周更本来就是全列覆盖。"""
        with self.db.transaction():
            for row in rows:
                self.db.execute(
                    "INSERT OR REPLACE INTO symbol_master"
                    " (market, code, name, py_initials, py_full, currency, is_active, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, true, now())",
                    [
                        market,
                        row["code"],
                        row["name"],
                        row["py_initials"],
                        row["py_full"],
                        row["currency"],
                    ],
                )

    def deactivate_missing(self, market: str, codes: list[str]) -> int:
        """把该市场当前活跃、却不在这份清单里的码关停；返回关停数。"""
        if not codes:
            return 0
        marks = ", ".join("?" for _ in codes)
        stale = self.db.scalar(
            f"SELECT count(*) FROM symbol_master WHERE market = ? AND is_active"
            f" AND code NOT IN ({marks})",
            [market, *codes],
        )
        self.db.execute(
            f"UPDATE symbol_master SET is_active = false, updated_at = now()"
            f" WHERE market = ? AND is_active AND code NOT IN ({marks})",
            [market, *codes],
        )
        return int(stale or 0)

    def get(self, market: str, code: str) -> dict | None:
        rows = self.db.query(
            "SELECT market, code, name, py_initials, py_full, currency, is_active"
            " FROM symbol_master WHERE market = ? AND code = ?",
            [market, code],
        )
        return None if rows.is_empty() else rows.to_dicts()[0]

    def search(self, q: str, limit: int = 10) -> list[dict]:
        """`q` 是归一好的检索词（大写片段）：py 列按小写比，code 按大写比。

        CTE `p` 只为了让 `?` 各出现一次——十来个重复 `?` 的实参表错位都错得安静。
        """
        rows = self.db.query(
            "WITH p AS (SELECT ? AS q_up, ? AS q_low)"
            " SELECT market, code, name, py_initials, py_full FROM ("
            "   SELECT s.market, s.code, s.name, s.py_initials, s.py_full,"
            "     CASE"
            "       WHEN s.code = p.q_up THEN 1"
            "       WHEN s.py_initials = p.q_low THEN 2"
            "       WHEN s.py_full = p.q_low THEN 3"
            "       WHEN starts_with(s.py_initials, p.q_low) OR starts_with(s.code, p.q_up) THEN 4"
            "       ELSE 5"
            "     END AS tier"
            "   FROM symbol_master s, p"
            "   WHERE s.is_active AND ("
            "     s.code = p.q_up OR s.py_initials = p.q_low OR s.py_full = p.q_low"
            "     OR starts_with(s.code, p.q_up) OR starts_with(s.py_initials, p.q_low)"
            "     OR contains(lower(s.name), p.q_low) OR contains(s.py_full, p.q_low))"
            ") ORDER BY tier, market, code LIMIT ?",
            [q.upper(), q.lower(), limit],
        )
        return rows.to_dicts()

    def count(self, market: str | None = None) -> int:
        if market is None:
            return int(self.db.scalar("SELECT count(*) FROM symbol_master WHERE is_active", []))
        return int(
            self.db.scalar(
                "SELECT count(*) FROM symbol_master WHERE is_active AND market = ?", [market]
            )
        )
