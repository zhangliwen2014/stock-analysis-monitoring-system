"""0031：摘掉 `symbol_master_search`（D230）。

DuckDB 2.0 的显式非唯一 ART 索引会持久化到坏状态（duckdb/duckdb#23645）：周更的
`INSERT OR REPLACE` 因此在 COMMIT 时报 "Failed to delete all rows from index"，整个
连接被标成 FATAL。那枚索引对检索从未有用（查询没有 `market` 等值条件、OR 跨九列，
计划里始终是 Sequential Scan），摘掉它，故障模式随之消失。
"""

from __future__ import annotations

import shutil

from sams.storage.db import Database
from sams.storage.migrate import MIGRATIONS_DIR, apply_migrations


def _indexes(db: Database) -> set[str]:
    return set(
        db.query(
            "SELECT index_name FROM duckdb_indexes() WHERE table_name = 'symbol_master'"
        )["index_name"].to_list()
    )


def test_0031_drops_the_search_index(db):
    assert "symbol_master_search" not in _indexes(db)


def test_an_old_database_loses_the_index_but_keeps_its_rows(tmp_path):
    legacy = tmp_path / "migrations"
    legacy.mkdir()
    for path in MIGRATIONS_DIR.glob("*.sql"):
        if path.name < "0031":
            shutil.copy(path, legacy / path.name)
    database = Database.open(str(tmp_path / "old.duckdb"))
    apply_migrations(database, legacy)
    database.execute(
        "INSERT INTO symbol_master (market, code, name, py_initials, py_full, currency)"
        " VALUES ('A', '600519', '贵州茅台', 'gzmt', 'guizhoumaotai', 'CNY')"
    )
    assert "symbol_master_search" in _indexes(database)

    shutil.copy(MIGRATIONS_DIR / "0031_drop_symbol_master_search_index.sql", legacy)
    assert apply_migrations(database, legacy) == ["0031_drop_symbol_master_search_index.sql"]

    assert "symbol_master_search" not in _indexes(database)
    assert database.query("SELECT code, name FROM symbol_master").to_dicts() == [
        {"code": "600519", "name": "贵州茅台"}
    ]
