"""0028：`financials` 加 14 个可空展示列（D217）。

只验形状与老库不丢行；适配器映射、五档筛选与页面行为各在自己的用例里。
"""

from __future__ import annotations

import shutil

from sams.storage.db import Database
from sams.storage.migrate import MIGRATIONS_DIR, apply_migrations

NEW_COLUMNS = {
    "report_type",
    "revenue",
    "net_profit",
    "eps",
    "bps",
    "ocf_per_share",
    "gross_margin",
    "net_margin",
    "roa",
    "roic",
    "debt_ratio",
    "current_ratio",
    "quick_ratio",
    "inventory_turnover",
    "total_asset_turnover",
}


def test_0028_adds_nullable_indicator_columns(db):
    columns = db.columns("financials")
    assert set(columns) >= NEW_COLUMNS
    rows = db.query(
        "SELECT column_name, is_nullable FROM information_schema.columns"
        " WHERE table_name = 'financials'"
    ).to_dicts()
    nullable = {row["column_name"]: row["is_nullable"] for row in rows}
    assert all(nullable[name] for name in NEW_COLUMNS), "新列必须可空：老行迁移前没有这些数"


def test_0028_keeps_existing_rows_on_an_older_database(tmp_path):
    legacy_dir = tmp_path / "migrations"
    legacy_dir.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name < "0028":
            shutil.copy(path, legacy_dir / path.name)

    database = Database.open(str(tmp_path / "legacy.duckdb"))
    try:
        apply_migrations(database, legacy_dir)
        database.execute(
            """
            INSERT INTO financials (market, code, report_date, published_date,
                                    roe, source)
            VALUES ('A', '600519', DATE '2025-12-31', DATE '2026-03-28', 0.35, 'akshare')
            """
        )
        applied = apply_migrations(database)
        assert "0028_financial_indicators.sql" in applied

        row = database.query(
            "SELECT roe, report_type, revenue FROM financials WHERE code = '600519'"
        ).to_dicts()[0]
        assert row["roe"] == 0.35
        assert row["report_type"] is None and row["revenue"] is None
    finally:
        database.close()
