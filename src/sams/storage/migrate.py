"""迁移驱动：按文件名顺序应用 migrations/*.sql，已应用的跳过（幂等）。

未来 A→B 换存储引擎时，改动范围限于 storage 包，本文件的机制可原样保留。
"""

from __future__ import annotations

from pathlib import Path

from sams.core.errors import StorageError
from sams.storage.db import Database

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _split_statements(sql: str) -> list[str]:
    """按语句切分。迁移文件不含字符串内分号，因此简单切分即可；
    遇到需要存储过程/引号内分号的场景，改为 sqlglot 解析而不是加启发式。
    """
    chunks = []
    for raw in sql.split(";"):
        body = "\n".join(
            line for line in raw.splitlines() if not line.strip().startswith("--")
        ).strip()
        if body:
            chunks.append(body)
    return chunks


def discover(migrations_dir: Path = MIGRATIONS_DIR) -> list[Path]:
    return sorted(migrations_dir.glob("*.sql"))


def applied(db: Database) -> set[str]:
    if "schema_migrations" not in db.table_names():
        return set()
    return set(db.query("SELECT name FROM schema_migrations")["name"].to_list())


def apply_migrations(db: Database, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """返回本次新应用的文件名；重复调用不会重复应用。"""
    done = applied(db)
    newly: list[str] = []
    for path in discover(migrations_dir):
        if path.name in done:
            continue
        sql = path.read_text(encoding="utf-8")
        for statement in _split_statements(sql):
            try:
                db.execute(statement)
            except Exception as exc:  # noqa: BLE001
                raise StorageError(f"迁移 {path.name} 失败: {exc}") from exc
        db.execute(
            "INSERT OR REPLACE INTO schema_migrations (name, applied_at) VALUES (?, now())",
            [path.name],
        )
        newly.append(path.name)
    return newly


def pending(db: Database, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    done = applied(db)
    return [p.name for p in discover(migrations_dir) if p.name not in done]
