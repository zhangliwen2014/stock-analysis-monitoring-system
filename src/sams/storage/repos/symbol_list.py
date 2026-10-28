"""`symbol_master` 的读写：全市场代码表，搜索加票的唯一入口。

五档排序（精确代码 > 首字母精确 > 全拼精确 > 前缀 > 片段）在一条 SQL 里定序：
拆成五段查询再 Python 合并，症状是每一档各自对、全局序错乱，而 LIMIT 10 截错边。
搜索永不 LIKE：`contains(lower(x), ?)` 是房规（见 `repos/annotations.py`）。
"""

from __future__ import annotations

import polars as pl

from sams.storage.db import Database

#: 集合写那六列（market 由调用方整批给一个值）：显式 schema 让「整列都是 None」也落定
#: 为 Utf8，而不是 polars 的 NULL 类型让 INSERT 报一句不带列名的转换错。
_BATCH_COLUMNS = ("market", "code", "name", "py_initials", "py_full", "currency")
_VIEW = "incoming_symbol_master"


class SymbolListRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert_batch(self, market: str, rows: list[dict]) -> None:
        """一次集合写覆盖全列：整表周更本来就是全列覆盖，逐行 INSERT 只是把它调成 N 次往返。

        走 `register` + `INSERT OR REPLACE ... SELECT`（`symbols.upsert` 的先例）；
        `is_active` 与 `updated_at` 由 SQL 侧给（常量 true 与 now()），不进 polars 帧。
        """
        if not rows:
            return
        frame = pl.DataFrame(
            {
                "market": [market] * len(rows),
                "code": [row["code"] for row in rows],
                "name": [row["name"] for row in rows],
                "py_initials": [row["py_initials"] for row in rows],
                "py_full": [row["py_full"] for row in rows],
                "currency": [row["currency"] for row in rows],
            },
            schema={c: pl.Utf8 for c in _BATCH_COLUMNS},
        )
        # 同一批里同码两次（源给重了行）时留最后一条：DuckDB 不会为「帧内部相撞」报约束错，
        # 于是「哪条胜出」全凭它内部的处理次序 —— 显式 dedupe 与 symbols._prepare 同规则
        frame = frame.unique(subset=["market", "code"], keep="last")
        self.db.register(_VIEW, frame)
        try:
            with self.db.transaction():
                self.db.execute(
                    "INSERT OR REPLACE INTO symbol_master"
                    " (market, code, name, py_initials, py_full, currency, is_active, updated_at)"
                    " SELECT market, code, name, py_initials, py_full, currency, true, now()"
                    f" FROM {_VIEW}"
                )
        finally:
            # 不放进 finally 的话，写失败的批次会把视图挂在连接上，下次同名注册读到残留
            self.db.unregister(_VIEW)

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
        # 空检索词在 contains/starts_with 下全表命中，空 py 列的无名票会落进第 2 档。
        # 调用方 normalize_query 已保证非空，这里是仓库层自守，不靠调用方的自觉。
        if not q.strip():
            return []
        rows = self.db.query(
            "WITH p AS (SELECT ? AS q_up, ? AS q_low)"
            " SELECT market, code, name, py_initials, py_full FROM ("
            "   SELECT s.market, s.code, s.name, s.py_initials, s.py_full,"
            "     CASE"
            "       WHEN s.code = p.q_up THEN 1"
            "       WHEN s.py_initials = p.q_low THEN 2"
            "       WHEN s.py_full = p.q_low THEN 3"
            "       WHEN starts_with(s.py_initials, p.q_low)"
            "         OR starts_with(s.code, p.q_up) OR starts_with(s.py_full, p.q_low) THEN 4"
            "       ELSE 5"
            "     END AS tier"
            "   FROM symbol_master s, p"
            "   WHERE s.is_active AND ("
            "     s.code = p.q_up OR s.py_initials = p.q_low OR s.py_full = p.q_low"
            "     OR starts_with(s.code, p.q_up) OR starts_with(s.py_initials, p.q_low)"
            "     OR starts_with(s.py_full, p.q_low)"
            "     OR contains(s.code, p.q_up)"
            "     OR contains(lower(s.name), p.q_low) OR contains(s.py_full, p.q_low))"
            ") ORDER BY tier, market, code LIMIT ?",
            [q.upper(), q.lower(), limit],
        )
        return rows.to_dicts()

    def active_codes(self, market: str) -> list[str]:
        """在市的全部代码，按代码升序：选股票池与快照请求都用这一份。"""
        frame = self.db.query(
            "SELECT code FROM symbol_master WHERE market = ? AND is_active ORDER BY code",
            [market],
        )
        return frame["code"].to_list()

    def count(self, market: str | None = None) -> int:
        if market is None:
            return int(self.db.scalar("SELECT count(*) FROM symbol_master WHERE is_active", []))
        return int(
            self.db.scalar(
                "SELECT count(*) FROM symbol_master WHERE is_active AND market = ?", [market]
            )
        )
