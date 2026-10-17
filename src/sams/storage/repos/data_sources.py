"""数据源配置仓库：`data_sources`（有哪些源）+ `data_source_routes`（每个市场谁先谁后）。

这一层只管**结构**：主键、名字写法、路由指向必须存在的源、优先级不重复。
「kind 是不是一个已注册实现」「停用它会不会让某个市场无源可用」这类判断需要认识
datasource 与 Settings，分层上不允许出现在 storage（`datasource` 与 `storage` 互不相干），
所以那些规则在 `services/sources.py`。

`chains()` 是采集与组装根读的唯一视图：只含启用中的源，按 priority 升序，
同一 priority 再按名字——降级顺序必须是一个确定的序列，不能取决于 DuckDB 的返回顺序。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from sams.core.errors import StorageError
from sams.storage.db import Database

#: 名字要进 CLI、进健康页、进日志，先规定成能被安全引用的形态
_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")


@dataclass(frozen=True)
class SourceRow:
    name: str
    kind: str
    enabled: bool = True
    options: dict | None = None
    note: str | None = None


class DataSourceRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ---------- 源本身 ----------

    def list(self) -> list[SourceRow]:
        if "data_sources" not in self.db.table_names():
            return []
        rows = self.db.query("SELECT * FROM data_sources ORDER BY name").to_dicts()
        return [_to_row(row) for row in rows]

    def get(self, name: str) -> SourceRow | None:
        if not self.list():
            return None
        rows = self.db.query("SELECT * FROM data_sources WHERE name = ?", [name]).to_dicts()
        return _to_row(rows[0]) if rows else None

    def upsert(self, row: SourceRow) -> SourceRow:
        _check_name(row.name)
        _check_name(row.kind, field="kind")
        if not row.kind:
            raise StorageError("data_sources: kind 不能为空")
        options = row.options or {}
        if not isinstance(options, dict):
            raise StorageError(f"data_sources: options 必须是映射，收到 {type(options).__name__}")
        self.db.execute(
            """
            INSERT OR REPLACE INTO data_sources
                (name, kind, enabled, options_json, note, updated_at)
            VALUES (?, ?, ?, ?, ?, now())
            """,
            [row.name, row.kind, row.enabled, json.dumps(options, ensure_ascii=False), row.note],
        )
        return row

    def set_enabled(self, name: str, enabled: bool) -> None:
        if self.get(name) is None:
            raise StorageError(f"数据源 {name} 不存在")
        self.db.execute(
            "UPDATE data_sources SET enabled = ?, updated_at = now() WHERE name = ?",
            [enabled, name],
        )

    def remove(self, name: str) -> int:
        """删源并连带删掉它的全部路由，返回被删的路由条数。

        留着指向已删源的路由等于埋一颗雷：`chains()` 会把它交给 registry，
        而那里报的是「配置了未注册的源」——一条没人配过的记录在下次启动时才炸。
        """
        if self.get(name) is None:
            raise StorageError(f"数据源 {name} 不存在")
        removed = len([r for r in self.routes() if r[1] == name])
        self.db.execute("DELETE FROM data_source_routes WHERE name = ?", [name])
        self.db.execute("DELETE FROM data_sources WHERE name = ?", [name])
        return removed

    # ---------- 每个市场的降级链 ----------

    def routes(self) -> list[tuple[str, str, int]]:
        """(market, name, priority)，含停用源——管理视图要看全，`chains()` 才筛。"""
        if "data_source_routes" not in self.db.table_names():
            return []
        rows = self.db.query(
            "SELECT market, name, priority FROM data_source_routes ORDER BY market, priority, name"
        ).to_dicts()
        return [(r["market"], r["name"], int(r["priority"])) for r in rows]

    def set_route(self, market: str, name: str, priority: int) -> None:
        if priority < 1:
            raise StorageError(f"priority 必须从 1 开始，收到 {priority}")
        if self.get(name) is None:
            raise StorageError(f"路由指向不存在的数据源 {name}：先加源，再排顺序")
        taken = [
            n
            for m, n, p in self.routes()
            if m == market and p == priority and n != name
        ]
        if taken:
            raise StorageError(
                f"市场 {market} 的第 {priority} 位已被 {taken} 占用；"
                f"要插入请走 reorder（整条链重排），别让两条路由抢同一个位置"
            )
        self.db.execute(
            """
            INSERT OR REPLACE INTO data_source_routes (market, name, priority)
            VALUES (?, ?, ?)
            """,
            [market, name, priority],
        )

    def reorder(self, market: str, names: list[str]) -> None:
        """把整条链按给定顺序重写成 1..n。

        逐条 set_route 会先撞上「优先级已被占用」——插入位和现有位天然冲突，
        所以重排必须是一次整体写入，而不是 n 次单点写入。
        """
        for name in names:
            if self.get(name) is None:
                raise StorageError(f"路由指向不存在的数据源 {name}")
        self.db.execute("DELETE FROM data_source_routes WHERE market = ?", [market])
        for index, name in enumerate(names, start=1):
            self.db.execute(
                "INSERT INTO data_source_routes (market, name, priority) VALUES (?, ?, ?)",
                [market, name, index],
            )

    def remove_route(self, market: str, name: str) -> bool:
        existed = any(m == market and n == name for m, n, _ in self.routes())
        self.db.execute(
            "DELETE FROM data_source_routes WHERE market = ? AND name = ?", [market, name]
        )
        return existed

    def chains(self) -> dict[str, tuple[str, ...]]:
        """市场 → 启用中的源名，按优先级升序。采集与组装根只认这个。"""
        enabled = {row.name for row in self.list() if row.enabled}
        ordered: dict[str, list[tuple[int, str]]] = {}
        for market, name, priority in self.routes():
            if name in enabled:
                ordered.setdefault(market, []).append((priority, name))
        return {
            market: tuple(name for _, name in sorted(items, key=lambda x: (x[0], x[1])))
            for market, items in ordered.items()
        }


def _to_row(row: dict) -> SourceRow:
    try:
        options = json.loads(row.get("options_json") or "{}")
    except json.JSONDecodeError as exc:
        raise StorageError(
            f"data_sources {row.get('name')} 的 options_json 不是合法 JSON: {exc}"
        ) from exc
    return SourceRow(
        name=row["name"],
        kind=row["kind"],
        enabled=bool(row["enabled"]),
        options=options if isinstance(options, dict) else {},
        note=row.get("note"),
    )


def _check_name(value: str, *, field: str = "name") -> None:
    if not _NAME.fullmatch(value or ""):
        raise StorageError(
            f"{field} 必须是小写字母开头的 1-32 位 [a-z0-9_-]，收到 {value!r}"
        )
