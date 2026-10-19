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

from sams.core.errors import StorageError

MEMORY = ":memory:"


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
            self._run("BEGIN TRANSACTION", None)
        self._tx_depth += 1
        try:
            yield
        except Exception:
            if self._tx_depth == 1:
                self._run("ROLLBACK", None)
            raise
        else:
            if self._tx_depth == 1:
                self._run("COMMIT", None)
        finally:
            self._tx_depth -= 1

    def execute(self, sql: str, params: list[Any] | None = None) -> None:
        self._run(sql, params)

    def _run(self, sql: str, params: list[Any] | None) -> Any:
        """唯一的 DuckDB ↔ StorageError 转换点。

        services 层按 `except StorageError` 兜仓库故障；漏出原始 duckdb 异常的话，
        约束冲突会一路冒到 CLI，运维看到的是一坨栈而不是「哪张表哪条约束」。
        """
        try:
            return self.conn.execute(sql, params or [])
        except duckdb.Error as exc:
            raise StorageError(f"SQL 执行失败: {exc}") from exc

    def query(self, sql: str, params: list[Any] | None = None) -> pl.DataFrame:
        return self._run(sql, params).pl()

    def scalar(self, sql: str, params: list[Any] | None = None) -> Any:
        row = self._run(sql, params).fetchone()
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

    def row_count(self, table: str) -> int:
        if table not in self.table_names():
            raise StorageError(f"表不存在: {table}")
        return int(self.scalar(f"SELECT count(*) FROM {table}") or 0)

    def close(self) -> None:
        self.conn.close()
