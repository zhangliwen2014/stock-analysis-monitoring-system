from __future__ import annotations

import pytest

from sams.core.errors import StorageError
from sams.storage.db import Database
from sams.storage.migrate import (
    applied,
    apply_migrations,
    checksum_of,
    discover,
    pending,
)

#: spec §5 的全部表；缺一张就说明迁移没写完
EXPECTED_TABLES = {
    "symbols",
    "bars_daily",
    "corp_actions",
    "quotes_snapshot",
    "funding_flow",
    "financials",
    "indices",
    "index_constituents",
    "scan_results",
    "trading_calendar",
    "fx_rates",
    "users",
    "user_sessions",
    "watchlists",
    "theses",
    "thesis_events",
    "valuation_snapshots",
    "rules",
    "rule_revisions",
    "channels",
    "alerts",
    "notify_log",
    "annotations",
    "annotation_revisions",
    "sim_accounts",
    "sim_cash_balances",
    "sim_orders",
    "sim_positions",
    "fx_trades",
    "task_runs",
    "schema_migrations",
    "source_health",
}


def test_migrations_create_every_spec_table(db):
    missing = EXPECTED_TABLES - set(db.table_names())
    assert not missing, f"缺表: {missing}"


def test_applying_twice_is_a_no_op(db):
    assert applied(db) == {p.name for p in discover()}
    assert pending(db) == []


def test_fresh_database_reports_pending_migration(tmp_path):
    fresh = Database.open(tmp_path / "fresh.duckdb")
    try:
        assert pending(fresh) == [p.name for p in discover()]
        assert applied(fresh) == set()
    finally:
        fresh.close()


def test_primary_key_makes_reinsert_idempotent(db):
    def upsert(name: str) -> None:
        db.execute(
            "INSERT OR REPLACE INTO symbols (market, code, name, currency)"
            " VALUES ('A', '600519', ?, 'CNY')",
            [name],
        )

    upsert("贵州茅台")
    upsert("茅台")
    rows = db.query("SELECT name FROM symbols WHERE market='A' AND code='600519'")
    assert rows.height == 1
    assert rows["name"][0] == "茅台", "同主键第二次应当覆盖而不是并存"


def test_unknown_table_row_count_raises(db):
    with pytest.raises(StorageError, match="表不存在"):
        db.row_count("nope")


def test_applied_migration_is_recorded_with_a_checksum(db):
    recorded = db.query("SELECT name, checksum FROM schema_migrations").to_dicts()
    assert all(row["checksum"] == checksum_of(next(p for p in discover() if p.name == row["name"]))
               for row in recorded), "每个已应用的迁移都要留下内容指纹"


def test_editing_an_applied_migration_refuses_to_run(tmp_path, db):
    """0001 全是 CREATE TABLE IF NOT EXISTS：改了它对老库永久无效，
    而 health 会说「已是最新」——两台机器两套 schema 就是这么来的。"""
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    source = next(p for p in discover() if p.name == "0001_init.sql")
    target = migrations / "0001_init.sql"
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    apply_migrations(db, migrations)

    target.write_text(
        target.read_text(encoding="utf-8") + "\nCREATE TABLE IF NOT EXISTS sneaky (x INT);\n",
        encoding="utf-8",
    )
    with pytest.raises(StorageError, match="已被修改"):
        apply_migrations(db, migrations)


def test_legacy_rows_without_checksum_are_backfilled(db):
    """校验和机制上线之前建的库没有指纹：补齐，而不是拒绝启动。"""
    db.execute("UPDATE schema_migrations SET checksum = NULL WHERE name = '0001_init.sql'")
    assert db.query(
        "SELECT checksum FROM schema_migrations WHERE name = '0001_init.sql'"
    )["checksum"][0] is None

    assert apply_migrations(db) == [], "没有新迁移要跑，回填不该被算作应用"
    rows = db.query("SELECT name, checksum FROM schema_migrations").to_dicts()
    assert all(row["checksum"] for row in rows), "老迁移的指纹应被回填"


def test_backfill_never_overwrites_an_existing_checksum(db):
    stored = db.query("SELECT checksum FROM schema_migrations WHERE name = '0001_init.sql'")[
        "checksum"
    ][0]
    apply_migrations(db)
    again = db.query("SELECT checksum FROM schema_migrations WHERE name = '0001_init.sql'")
    assert again["checksum"][0] == stored, "回填只补空，绝不覆盖——否则篡改会被静默洗白"
    first = next(p for p in discover() if p.name == "0001_init.sql")
    assert stored == checksum_of(first)


def test_upgrade_from_pre_semantics_database_with_rows(tmp_path):
    """在「0003 之前 + 已有行情数据」的库上升级：重建 bars_daily 不能丢行、不能改口径。

    这是最可能在生产出事的一条路径：迁移在空库上跑绿不代表带库升级也成立。
    """
    old_dir = tmp_path / "old"
    old_dir.mkdir()
    for name in ("0001_init.sql", "0002_source_health.sql"):
        source = next(p for p in discover() if p.name == name)
        (old_dir / name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    path = tmp_path / "prod.duckdb"
    legacy = Database.open(path)
    try:
        assert apply_migrations(legacy, old_dir) == ["0001_init.sql", "0002_source_health.sql"]
        legacy.execute(
            "INSERT INTO bars_daily (market, code, date, close, volume, source)"
            " VALUES ('A', '600519', DATE '2024-01-02', 1810.0, 30000, 'akshare')"
        )

        # 同一份数据文件，用当前迁移集再打开一次：升级必须无损
        legacy.close()
        upgraded = Database.open(path)
        try:
            newly = apply_migrations(upgraded)
            assert [p.name for p in discover()[2:]] == newly, "老库补齐后续迁移"
            rows = upgraded.query("SELECT * FROM bars_daily").to_dicts()
            assert len(rows) == 1
            assert rows[0]["adjust"] == "raw", "历史行要归到不复权口径，不能留在 NULL"
            assert rows[0]["close"] == 1810.0
            assert "adj_factor" not in rows[0]
            assert "corp_actions" in upgraded.table_names()
            recorded = upgraded.query(
                "SELECT name, checksum FROM schema_migrations"
            ).to_dicts()
            assert all(row["checksum"] for row in recorded), "升级后每个迁移都有指纹"
        finally:
            upgraded.close()
    finally:
        if path.exists():
            path.unlink()
