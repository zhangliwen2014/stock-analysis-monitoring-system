"""0019：盘中报价的形状 + 测试告警 + 清理对象的索引。

这一枚只验形状与默认值：四个价格列可空、`is_test` 在旧行与新行上都读得到 False、
`sina_quote` 排在每个市场链末位。行为契约分别在 `test_quotes_repo.py`（Task 8）
与 `test_prune.py`（Task 13）。
"""

from __future__ import annotations

import shutil

from sams.storage.db import Database
from sams.storage.migrate import MIGRATIONS_DIR, apply_migrations
from sams.storage.repos.data_sources import DataSourceRepo


def insert_alert(db: Database, alert_id: str) -> None:
    db.execute(
        "INSERT INTO alerts (id, user_id, market, code, stage, payload_json)"
        " VALUES (?, 'u1', 'A', '600519', 'entry', '{}')",
        [alert_id],
    )


def test_0019_adds_quote_columns_and_health_table(db):
    assert {"open", "prev_close", "high", "low"} <= db.columns("quotes_snapshot")
    assert {"display_name"} <= db.columns("users")
    assert {"is_test"} <= db.columns("alerts")
    assert "quote_source_health" in db.table_names()


def test_0019_seeds_sina_quote_at_the_end_of_every_market(db):
    repo = DataSourceRepo(db)
    row = repo.get("sina_quote")
    assert row is not None and row.kind == "sina_quote"
    assert repo.chains() == {
        "A": ("akshare", "eastmoney", "tencent", "sina", "akshare_ths", "sina_quote"),
        "HK": ("yfinance", "tencent", "akshare_hk", "sina_quote"),
        "US": ("yfinance", "tencent", "sina_quote"),
    }


def test_is_test_defaults_to_false_on_new_rows(db):
    insert_alert(db, "a1")
    assert db.scalar("SELECT is_test FROM alerts WHERE id = 'a1'") is False


def test_0019_backfills_is_test_on_an_existing_database(tmp_path):
    """DuckDB 不支持 `ADD COLUMN ... NOT NULL DEFAULT`，所以 0019 走「加列 → 设默认
    → 回填」。这三步少任何一步，旧库上都会出现 NULL 或默认值没生效；只在新库上测
    不出来，必须真的先建一个停在 0018 的库。
    """
    legacy_dir = tmp_path / "migrations"
    legacy_dir.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name < "0019":
            shutil.copy(path, legacy_dir / path.name)

    database = Database.open(str(tmp_path / "legacy.duckdb"))
    try:
        apply_migrations(database, legacy_dir)
        insert_alert(database, "old")
        newly = apply_migrations(database)
        assert "0019_quotes_and_prune.sql" in newly
        assert database.scalar("SELECT is_test FROM alerts WHERE id = 'old'") is False
        # 迁移之后由服务层漏传这一列的行也必须拿到 False，而不是 NULL
        insert_alert(database, "new")
        assert database.scalar("SELECT is_test FROM alerts WHERE id = 'new'") is False
    finally:
        database.close()
