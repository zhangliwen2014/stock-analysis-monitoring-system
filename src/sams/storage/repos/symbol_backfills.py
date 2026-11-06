"""`symbol_backfills` 的读写：新票的补数排队簿。

一行是「某只票欠某一种活」：`kind` 有 `bars`（补日线）与 `actions`（确认公司行为覆盖），
两者是不同的账，同一票可以各挂一条开放行。`open_key`（`'{market}|{code}|{kind}'`，结案置
NULL）是去重键：DuckDB 没有 partial unique index，唯一索引里 NULL 互不冲突，于是
「同一票同一活开放期只许一条」就靠非空那一份。
状态只有三个：pending（可试）、deferred（今日试满 3 次，让位盘后任务）、done。

D330 之后 `kind` 又多三种：`financials` / `valuation` / `flow`。它们不是「页面点了补采」，
而是孤源 job 止损时把没跑的票整批顺延下来的（见 `scheduler/jobs.py::_collect`），
初始状态直接是 deferred。
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

#: 队列里的两种活。默认给 `bars`：既有调用方（自选、看板、成交）说的是「这票一根 K 线都没有」，
#: 加列之前那就是队列唯一的一种活（spec 2026-11-03 §3.3）
BARS = "bars"
ACTIONS = "actions"

#: D330：孤源止损的顺延账。名字跟 job 走（`_DATA_JOBS` 里的 financials/valuation/flow），
#: 消费者按同一个名字分派。
FINANCIALS = "financials"
VALUATION = "valuation"
FLOW = "flow"


@dataclass(frozen=True)
class BackfillRow:
    id: str
    market: str
    code: str
    tries: int
    last_error: str | None
    status: str
    kind: str = BARS


#: `BackfillRow` 的列清单：take_pending 与 open_for 读的是同一个形状，
#: 两处各写一遍时，加一列只需要改一个地方
_OPEN_COLUMNS = "id, market, code, tries, last_error, status, kind"


class SymbolBackfillRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def enqueue(self, market: str, code: str, kind: str = BARS) -> bool:
        """排上返回 True；同一票的同一种活已在开放队列里返回 False（不重复排队）。"""
        key = f"{market}|{code}|{kind}"
        with self.db.transaction():
            # 查后插放在一个事务里：两个并发都看见 None 时，唯一索引是最后的兜底
            existing = self.db.scalar(
                "SELECT open_key FROM symbol_backfills WHERE open_key = ?", [key]
            )
            if existing is not None:
                return False
            self.db.execute(
                "INSERT INTO symbol_backfills"
                " (id, market, code, tries, status, open_key, created_at, kind)"
                " VALUES (?, ?, ?, 0, 'pending', ?, now(), ?)",
                [uuid4().hex, market, code, key, kind],
            )
        return True

    def defer(self, market: str, code: str, kind: str, reason: str) -> bool:
        """直接排成一行的 `deferred`（D330 的孤源止损）。

        不先 `enqueue` 再记三次失败：那样 `tries` 会写成 3，读起来像消费者真试过三次，
        实际一次都没有。开放行已经存在时返回 False（去重键与 `enqueue` 同一条）。
        """
        key = f"{market}|{code}|{kind}"
        first_line = (reason.splitlines() or [""])[0][:200]
        with self.db.transaction():
            existing = self.db.scalar(
                "SELECT open_key FROM symbol_backfills WHERE open_key = ?", [key]
            )
            if existing is not None:
                return False
            self.db.execute(
                "INSERT INTO symbol_backfills"
                " (id, market, code, tries, status, open_key, created_at, kind, last_error)"
                " VALUES (?, ?, ?, 0, ?, ?, now(), ?, ?)",
                [uuid4().hex, market, code, DEFERRED, key, kind, first_line],
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

    def mark_done_by_code(self, market: str, code: str, kind: str = BARS) -> None:
        """盘后日线任务替补数队列收尾：只要成功写进行，开放的那条就算结清。

        按 kind 结：日线写成功了就把同一票的「公司行为待确认」也标成 done，那条覆盖就永远
        补不上了——而界面上刚说过的「后台已排队」变成假话。
        """
        self.db.execute(
            "UPDATE symbol_backfills SET status = 'done', open_key = NULL, done_at = now()"
            " WHERE market = ? AND code = ? AND kind = ? AND open_key IS NOT NULL",
            [market, code, kind],
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

    def rearm_deferred(self, kind: str) -> int:
        """把某种活顺延到明天的行重新排上，返回顺延了几条（新的重试预算）。

        `deferred` 那句话的承诺是「今天的预算用光」，所以跨天必须有人把它兑掉：日线有盘后任务
        结案，而公司行为的今晚 job 只跑 universe（自选/持仓/规则/论点），队列里那条非 universe
        的行不然会永远躺在 deferred（spec 2026-11-03 §3.4）。
        """
        found = self.db.scalar(
            "SELECT count(*) FROM symbol_backfills WHERE status = ? AND kind = ?",
            [DEFERRED, kind],
        )
        self.db.execute(
            "UPDATE symbol_backfills SET status = ?, tries = 0, last_error = NULL"
            " WHERE status = ? AND kind = ?",
            [PENDING, DEFERRED, kind],
        )
        return int(found or 0)

    def open_for(self, market: str, code: str, kind: str = BARS) -> BackfillRow | None:
        rows = self.db.query(
            f"SELECT {_OPEN_COLUMNS} FROM symbol_backfills WHERE open_key = ?",
            [f"{market}|{code}|{kind}"],
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
