"""DuckDB 连接包装。

仓库层是唯一的读写入口（spec D5），所以 Polars ↔ DuckDB 的转换点也只收在这里：
上层拿到的是 pl.DataFrame，看不到 Arrow 注册细节。
"""

from __future__ import annotations

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

    @classmethod
    def open(cls, path: str | Path) -> Database:
        target = str(path)
        if target != MEMORY:
            Path(target).parent.mkdir(parents=True, exist_ok=True)
        try:
            return cls(duckdb.connect(target), target)
        except duckdb.Error as exc:
            raise StorageError(f"无法打开数据库 {target}: {exc}") from exc

    def execute(self, sql: str, params: list[Any] | None = None) -> None:
        self.conn.execute(sql, params or [])

    def query(self, sql: str, params: list[Any] | None = None) -> pl.DataFrame:
        return self.conn.execute(sql, params or []).pl()

    def register(self, name: str, df: pl.DataFrame) -> None:
        self.conn.register(name, df.to_arrow())

    def unregister(self, name: str) -> None:
        """注册视图是连接级的、会一直挂着：不注销的话，下一批数据可能读到上一批的残留。"""
        self.conn.unregister(name)

    def scalar(self, sql: str, params: list[Any] | None = None) -> Any:
        row = self.conn.execute(sql, params or []).fetchone()
        return row[0] if row else None

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
