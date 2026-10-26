"""DuckDB 连接包装。

仓库层是唯一的读写入口（spec D5），所以 Polars ↔ DuckDB 的转换点也只收在这里：
上层拿到的是 pl.DataFrame，看不到 Arrow 注册细节。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import duckdb
import polars as pl

from sams.core.errors import DatabaseLocked, StorageError

MEMORY = ":memory:"
#: DuckDB 那句冲突锁的两种稳定片段。只认文本是因为 `duckdb.Error` 没有子类型区分
#: 「锁」与「权限/损坏」；认错会让一个真正的坏文件被报成「等一下就好」。
_LOCK_MARKERS = ("Could not set lock on file", "Conflicting lock")


def _is_lock_error(exc: duckdb.Error) -> bool:
    text = str(exc)
    return any(marker in text for marker in _LOCK_MARKERS)


class Database:
    def __init__(self, conn: duckdb.DuckDBPyConnection, path: str = MEMORY) -> None:
        self.conn = conn
        #: 记住来源，健康报告与日志要显示「这个库是哪个文件」
        self.path = path
        self._tx_depth = 0

    @classmethod
    def open(cls, path: str | Path) -> Database:
        target = str(path)
        if target != MEMORY:
            Path(target).parent.mkdir(parents=True, exist_ok=True)
        try:
            return cls(duckdb.connect(target), target)
        except duckdb.Error as exc:
            if _is_lock_error(exc):
                raise DatabaseLocked(target, detail=str(exc)) from exc
            raise StorageError(f"无法打开数据库 {target}: {exc}") from exc

    @property
    def in_transaction(self) -> bool:
        """是否已经在一个事务块里（重入计数大于 0）。仓库层用它决定要不要自己开。"""
        return self._tx_depth > 0

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """一个原子写单元：块内任何异常都整段回滚，包括调用方没接住的。

        账本的一笔卖出同时写 `sim_orders`、`sim_positions`、`sim_cash_balances`、
        `sim_cash_moves`：中途失败留下的「现金已扣、持仓未减」不是待修的脏数据，
        是整本账失去意义。重入时内层只是加入外层那个事务（DuckDB 不支持嵌套 BEGIN），
        所以任何一层抛异常，最外层统一回滚——「谁负责原子性」由第一个开块的人定，
        不由谁碰巧写了 BEGIN 定。迁移有自己的 BEGIN/COMMIT（`migrate.py`），
        那是 DDL 的引导路径，不需要也不该走这里。
        """
        if self._tx_depth == 0:
            self.execute("BEGIN TRANSACTION")
        self._tx_depth += 1
        try:
            yield
        except Exception:
            if self._tx_depth == 1:
                self.execute("ROLLBACK")
            raise
        else:
            if self._tx_depth == 1:
                self.execute("COMMIT")
        finally:
            self._tx_depth -= 1

    def execute(self, sql: str, params: list[Any] | None = None) -> None:
        # duckdb 2.0 收紧了同连接上的「未完成结果」：结果没 drain 就 register/query
        # 会 ResourceInUse。这里没人读结果，fetchall 就是把空结果排干。
        self._run(sql, params).fetchall()

    def _run(self, sql: str, params: list[Any] | None) -> Any:
        """唯一的 DuckDB ↔ StorageError 转换点。

        services 层按 `except StorageError` 兜仓库故障；漏出原始 duckdb 异常的话，
        约束冲突会一路冒到 CLI，运维看到的是一坨栈而不是「哪张表哪条约束」。

        返回的是**未 drain 的结果**：drain 由读方负责（query/scalar/execute 各自
        在读完后排干）。complete()/fetchall() 若发生在读取之前会让结果失效。
        """
        try:
            return self.conn.execute(sql, params or [])
        except duckdb.Error as exc:
            raise StorageError(f"SQL 执行失败: {exc}") from exc

    def query(self, sql: str, params: list[Any] | None = None) -> pl.DataFrame:
        # duckdb 2.0 把 execute 结果上的 `.pl()` 拿掉了（polars 转换挪到 relation）；
        # `con.sql` 又不收参数化的位置参——所以留在 `_run`（锁错误翻译在那条路上）走 arrow。
        result = self._run(sql, params)
        frame = pl.from_arrow(result.to_arrow_table())
        result.fetchall()
        return frame

    def scalar(self, sql: str, params: list[Any] | None = None) -> Any:
        result = self._run(sql, params)
        row = result.fetchone()
        result.fetchall()
        return row[0] if row else None

    def register(self, name: str, df: pl.DataFrame) -> None:
        self.conn.register(name, df.to_arrow())

    def unregister(self, name: str) -> None:
        """注册视图是连接级的、会一直挂着：不注销的话，下一批数据可能读到上一批的残留。"""
        self.conn.unregister(name)

    def table_names(self) -> list[str]:
        rows = self.query(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
        )
        return sorted(rows["table_name"].to_list()) if not rows.is_empty() else []

    def columns(self, table: str) -> set[str]:
        """一张表的列名集合。

        健康报告用它做护栏：`0015` 之前建的库没有 `outcome` 那一列，按列名的 SQL 会当场
        报错，而「通知好不好用」这个问题不该让整份健康报告跑不起来。
        """
        rows = self.query(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'main' AND table_name = ?",
            [table],
        )
        return set(rows["column_name"].to_list()) if not rows.is_empty() else set()

    def row_count(self, table: str) -> int:
        if table not in self.table_names():
            raise StorageError(f"表不存在: {table}")
        return int(self.scalar(f"SELECT count(*) FROM {table}") or 0)

    def close(self) -> None:
        self.conn.close()
