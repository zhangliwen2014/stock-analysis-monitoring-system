from __future__ import annotations

import pytest

from sams.core.errors import StorageError
from sams.storage.db import Database
from sams.storage.migrate import applied, discover, pending

#: spec §5 的全部表；缺一张就说明迁移没写完
EXPECTED_TABLES = {
    "symbols",
    "bars_daily",
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
