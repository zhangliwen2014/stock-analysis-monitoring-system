"""迁移驱动：按文件名顺序应用 migrations/*.sql，已应用的跳过（幂等）。

未来 A→B 换存储引擎时，改动范围限于 storage 包，本文件的机制可原样保留。

每个文件记 sha256 校验和：0001 全是 `CREATE TABLE IF NOT EXISTS`，只记文件名的话，
改一次已应用的迁移会「新库生效、老库永不生效」，而 health 还报告已是最新——
两台机器跑两套 schema 是最难查的那类事故，所以这里选择启动即失败。
0004 之前的库没有指纹，改用「重放迁移文件比对 schema」这条可证伪的路。
每个文件包在一个事务里：中途失败整体回滚，改好文件就能重试。
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

from sams.core.errors import StorageError
from sams.storage.db import MEMORY, Database

logger = logging.getLogger("sams.storage.migrate")

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_NAME = re.compile(r"\d{4}_[A-Za-z0-9._-]+\.sql")


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
    """迁移按文件名顺序执行：四位零填充序号 + 短描述，如 `0007_add_xxx.sql`。

    序号不补零时 `10_` 会排在 `1_` 之前（依赖就此颠倒，且只在某个特定的库上才炸），
    序号重复时两个文件共用一条 `schema_migrations` 记录——所以名字不合格就启动即失败。
    """
    paths = sorted(migrations_dir.glob("*.sql"))
    bad = [p.name for p in paths if not _NAME.match(p.name)]
    if bad:
        raise StorageError(
            f"迁移文件名必须是不重复的四位数字前缀（如 0007_xxx.sql），不合规: {sorted(bad)}"
        )
    numbers = [int(p.name[:4]) for p in paths]
    duplicated = sorted({n for n in numbers if numbers.count(n) > 1})
    if duplicated:
        raise StorageError(f"迁移序号重复: {duplicated} —— 每个迁移必须占一个唯一序号")
    return paths


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
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'main' AND table_name = 'schema_migrations'"
    )
    return "checksum" in columns["column_name"].to_list()


def _verify_unchanged(recorded: dict[str, str | None], paths: list[Path]) -> None:
    """已应用的迁移内容变了就拒绝继续——改历史要另开一个迁移文件。"""
    for path in paths:
        stored = recorded.get(path.name)
        if stored is None:
            continue  # 老库没记校验和，无法证伪；由 _verify_against_replay 兜底
        current = checksum_of(path)
        if current != stored:
            raise StorageError(
                f"迁移 {path.name} 已被修改（库内校验和 {stored[:12]}…，当前 {current[:12]}…）。"
                "已应用的迁移不可改，请新增迁移文件。"
            )


def _verify_no_missing_file(recorded: dict[str, str | None], paths: list[Path]) -> None:
    """库里有记录、目录里没文件：静默通过的话，把文件补回来的那天它也不会再执行。"""
    missing = sorted(set(recorded) - {p.name for p in paths})
    if missing:
        raise StorageError(f"迁移目录缺少库里已记录的文件: {missing}")


def _schema_signature(db: Database) -> dict[str, set[str]]:
    rows = db.query(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = 'main'"
    ).to_dicts()
    signature: dict[str, set[str]] = {}
    for row in rows:
        signature.setdefault(row["table_name"], set()).add(row["column_name"])
    return signature


def _verify_against_replay(
    db: Database, recorded: dict[str, str | None], paths: list[Path]
) -> None:
    """没有指纹就无法比对内容，于是换个可证伪的角度：把库里记为已应用的那几个文件
    在内存库重放一遍，凡是重放有、真库没有的表或列，说明文件与库的历史不一致
    ——「本地能跑、生产缺列」正是这么来的。只在存在无指纹记录时才花这个钱。
    """
    replayed = [p for p in paths if p.name in recorded]
    probe = Database.open(MEMORY)
    try:
        for path in replayed:
            for statement in _split_statements(path.read_text(encoding="utf-8")):
                probe.execute(statement)
        expected, actual = _schema_signature(probe), _schema_signature(db)
    except StorageError:
        raise
    finally:
        probe.close()
    drift = sorted(
        [table for table in expected if table not in actual]
        + [
            f"{table}.{column}"
            for table, columns in expected.items()
            for column in columns
            if table in actual and column not in actual[table]
        ]
    )
    if drift:
        raise StorageError(
            f"库里已应用的迁移缺少校验和，无法比对内容；重放迁移文件得到的 "
            f"{drift[:8]} 在库里不存在 → 迁移文件被改动过，或这个库不是这套迁移建的。"
        )


def apply_migrations(db: Database, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """返回本次新应用的文件名；重复调用不会重复应用。"""
    paths = discover(migrations_dir)
    recorded = _recorded(db)
    _verify_unchanged(recorded, paths)
    _verify_no_missing_file(recorded, paths)
    if any(checksum is None for checksum in recorded.values()):
        _verify_against_replay(db, recorded, paths)
    newly: list[str] = []
    for path in paths:
        if path.name in recorded:
            continue
        _apply_one(db, path)
        newly.append(path.name)
    _backfill_checksums(db, paths, set(newly))
    return newly


def _apply_one(db: Database, path: Path) -> None:
    """一个文件一个事务。

    逐语句 autocommit 时中途失败会留下半个 schema 且再也重试不回去：0003 的
    `CREATE TABLE bars_daily_new` 没有 IF NOT EXISTS，第二次执行必撞 already exists，
    只能人工删表。DuckDB 的 DDL 是事务性的，所以整体回滚后改好文件即可重来。
    """
    db.execute("BEGIN TRANSACTION")
    try:
        for statement in _split_statements(path.read_text(encoding="utf-8")):
            db.execute(statement)
        _record(db, path)
    except Exception as exc:  # noqa: BLE001
        db.execute("ROLLBACK")
        raise StorageError(f"迁移 {path.name} 失败: {exc}") from exc
    db.execute("COMMIT")


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


def _backfill_checksums(db: Database, paths: list[Path], just_applied: set[str]) -> None:
    """把没有指纹的行补上：老库升上来、以及本次运行中途才加列的情形都靠它。只回填，不覆盖。"""
    if not _has_checksum_column(db):
        return
    by_name = {p.name: p for p in paths}
    missing = db.query(
        "SELECT name FROM schema_migrations WHERE checksum IS NULL"
    )["name"].to_list()
    for name in missing:
        db.execute(
            "UPDATE schema_migrations SET checksum = ? WHERE name = ?",
            [checksum_of(by_name[name]), name],
        )
        if name in just_applied:
            logger.info("迁移 %s 本次应用，校验和已登记", name)
        else:
            # 库里早有记录却没有指纹：只能拿当前内容当基线，这是唯一的洗白入口，
            # 所以它必须吵——一致性由 apply_migrations 里的重放比对负责把关
            logger.warning("迁移 %s 此前没有指纹，已按当前内容登记", name)


def pending(db: Database, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    done = applied(db)
    return [p.name for p in discover(migrations_dir) if p.name not in done]
