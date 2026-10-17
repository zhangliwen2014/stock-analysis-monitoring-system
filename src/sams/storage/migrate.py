"""迁移驱动：按文件名顺序应用 migrations/*.sql，已应用的跳过（幂等）。

未来 A→B 换存储引擎时，改动范围限于 storage 包，本文件的机制可原样保留。

每个文件记 sha256 校验和：0001 全是 `CREATE TABLE IF NOT EXISTS`，只记文件名的话，
改一次已应用的迁移会「新库生效、老库永不生效」，而 health 还报告已是最新——
两台机器跑两套 schema 是最难查的那类事故，所以这里选择启动即失败。
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from sams.core.errors import StorageError
from sams.storage.db import Database

logger = logging.getLogger("sams.storage.migrate")

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


def checksum_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def applied(db: Database) -> set[str]:
    return set(_recorded(db))


def _recorded(db: Database) -> dict[str, str | None]:
    """已应用迁移 → 校验和。库还没建 schema_migrations 时返回空。"""
    if "schema_migrations" not in db.table_names():
        return {}
    if not _has_checksum_column(db):
        # 0004 之前的库只记了文件名：先让迁移跑起来，本轮结束时补齐校验和
        return dict.fromkeys(db.query("SELECT name FROM schema_migrations")["name"].to_list())
    rows = db.query("SELECT name, checksum FROM schema_migrations").to_dicts()
    return {row["name"]: row["checksum"] for row in rows}


def _has_checksum_column(db: Database) -> bool:
    columns = db.query(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'schema_migrations'"
    )
    return "checksum" in columns["column_name"].to_list()


def _verify_unchanged(recorded: dict[str, str | None], paths: list[Path]) -> None:
    """已应用的迁移内容变了就拒绝继续——改历史要另开一个迁移文件。"""
    for path in paths:
        stored = recorded.get(path.name)
        if stored is None:
            continue  # 老库没记校验和，无法证伪；应用后补上
        current = checksum_of(path)
        if current != stored:
            raise StorageError(
                f"迁移 {path.name} 已被修改（库内校验和 {stored[:12]}…，当前 {current[:12]}…）。"
                "已应用的迁移不可改，请新增迁移文件。"
            )


def apply_migrations(db: Database, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """返回本次新应用的文件名；重复调用不会重复应用。"""
    paths = discover(migrations_dir)
    recorded = _recorded(db)
    _verify_unchanged(recorded, paths)
    newly: list[str] = []
    for path in paths:
        if path.name in recorded:
            continue
        sql = path.read_text(encoding="utf-8")
        for statement in _split_statements(sql):
            try:
                db.execute(statement)
            except Exception as exc:  # noqa: BLE001
                raise StorageError(f"迁移 {path.name} 失败: {exc}") from exc
        _record(db, path)
        newly.append(path.name)
    _backfill_checksums(db, paths)
    return newly


def _record(db: Database, path: Path) -> None:
    if _has_checksum_column(db):
        db.execute(
            "INSERT OR REPLACE INTO schema_migrations (name, applied_at, checksum) "
            "VALUES (?, now(), ?)",
            [path.name, checksum_of(path)],
        )
    else:
        # 校验和列是 0004 才加的，在它之前的迁移只能先记文件名，本轮末尾统一补齐
        db.execute(
            "INSERT OR REPLACE INTO schema_migrations (name, applied_at) VALUES (?, now())",
            [path.name],
        )


def _backfill_checksums(db: Database, paths: list[Path]) -> None:
    """把没有指纹的行补上：老库升上来、以及本次运行中途才加列的情形都靠它。只回填，不覆盖。"""
    if not _has_checksum_column(db):
        return
    by_name = {p.name: p for p in paths}
    missing = db.query(
        "SELECT name FROM schema_migrations WHERE checksum IS NULL"
    )["name"].to_list()
    for name in missing:
        path = by_name.get(name)
        if path is None:
            logger.warning("迁移 %s 在库里有记录但文件已不在，跳过校验和回填", name)
            continue
        db.execute(
            "UPDATE schema_migrations SET checksum = ? WHERE name = ?",
            [checksum_of(path), name],
        )
        logger.info("迁移 %s 的校验和已补齐（此前无记录）", name)


def pending(db: Database, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    done = applied(db)
    return [p.name for p in discover(migrations_dir) if p.name not in done]
