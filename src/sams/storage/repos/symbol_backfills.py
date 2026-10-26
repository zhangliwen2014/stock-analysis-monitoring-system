"""`symbol_backfills` 的读写：新票的补数排队簿。

`open_key`（`'{market}|{code}'`，结案置 NULL）是去重键：DuckDB 没有 partial
unique index，唯一索引里 NULL 互不冲突，于是「开放期只许一条」就靠非空那一份。
状态只有三个：pending（可试）、deferred（今日试满 3 次，让位盘后任务）、done。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sams.storage.db import Database

#: 补数行两个状态的对外名（先例：repos/theses.py 导出 OPENED/TRACKING）：Python 侧的
#: 调用方不必各写各的字符串；done 只在仓库 SQL 里落，没有 Python 侧的读写方就不导出，
#: 仓库内部 SQL 里的字面量保持原样（那就是本仓库的落库形态）
PENDING = "pending"
DEFERRED = "deferred"


@dataclass(frozen=True)
class BackfillRow:
    id: str
    market: str
    code: str
    tries: int
    last_error: str | None
    status: str


#: `BackfillRow` 的列清单：take_pending 与 open_for 读的是同一个形状，
#: 两处各写一遍时，加一列只需要改一个地方
_OPEN_COLUMNS = "id, market, code, tries, last_error, status"


class SymbolBackfillRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def enqueue(self, market: str, code: str) -> bool:
        """排上返回 True；同一票已在开放队列里返回 False（不重复排队）。"""
        key = f"{market}|{code}"
        with self.db.transaction():
            # 查后插放在一个事务里：两个并发都看见 None 时，唯一索引是最后的兜底
            existing = self.db.scalar(
                "SELECT open_key FROM symbol_backfills WHERE open_key = ?", [key]
            )
            if existing is not None:
                return False
            self.db.execute(
                "INSERT INTO symbol_backfills"
                " (id, market, code, tries, status, open_key, created_at)"
                " VALUES (?, ?, ?, 0, 'pending', ?, now())",
                [uuid4().hex, market, code, key],
            )
        return True

    def take_pending(self, limit: int = 3) -> list[BackfillRow]:
        # 不回读 created_at：TIMESTAMPTZ 取回不稳（房规），这里只需要它排序
        rows = self.db.query(
            f"SELECT {_OPEN_COLUMNS} FROM symbol_backfills"
            " WHERE status = 'pending' ORDER BY created_at, id LIMIT ?",
            [limit],
        )
        return [BackfillRow(**row) for row in rows.to_dicts()]

    def mark_done(self, row_id: str) -> None:
        self.db.execute(
            "UPDATE symbol_backfills SET status = 'done', open_key = NULL, done_at = now()"
            " WHERE id = ?",
            [row_id],
        )

    def mark_done_by_code(self, market: str, code: str) -> None:
        """盘后日线任务替补数队列收尾：只要成功写进行，开放的那条就算结清。"""
        self.db.execute(
            "UPDATE symbol_backfills SET status = 'done', open_key = NULL, done_at = now()"
            " WHERE market = ? AND code = ? AND open_key IS NOT NULL",
            [market, code],
        )

    def note_failure(self, row_id: str, error: str, *, max_tries: int = 3) -> str:
        """记一次失败，返回新状态：pending 或 deferred。

        多行错误（traceback）只存第一行：`last_error` 是给「这票为什么没补上」
        那一格看的一句话，不是日志转储。
        """
        first_line = (error.splitlines() or [""])[0][:200]
        with self.db.transaction():
            self.db.execute(
                "UPDATE symbol_backfills SET tries = tries + 1, last_error = ?,"
                " status = CASE WHEN tries + 1 >= ? THEN 'deferred' ELSE status END"
                " WHERE id = ?",
                [first_line, max_tries, row_id],
            )
            status = self.db.scalar("SELECT status FROM symbol_backfills WHERE id = ?", [row_id])
        if status is None:
            raise ValueError(f"补数记录 {row_id} 不存在")
        return str(status)

    def open_for(self, market: str, code: str) -> BackfillRow | None:
        rows = self.db.query(
            f"SELECT {_OPEN_COLUMNS} FROM symbol_backfills WHERE open_key = ?",
            [f"{market}|{code}"],
        )
        return None if rows.is_empty() else BackfillRow(**rows.to_dicts()[0])

    def count_done_before(self, cutoff: datetime) -> int:
        """只数结案早于 `cutoff` 的 done 行（判据 <，与 task_runs 同一族）：
        pending/deferred 带着 `open_key` 语义，保留策略永远不动它们。"""
        return int(
            self.db.scalar(
                "SELECT count(*) FROM symbol_backfills WHERE status = 'done' AND done_at < ?",
                [cutoff],
            )
            or 0
        )

    def delete_done_before(self, cutoff: datetime) -> None:
        self.db.execute(
            "DELETE FROM symbol_backfills WHERE status = 'done' AND done_at < ?",
            [cutoff],
        )
