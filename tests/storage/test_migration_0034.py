"""0034：告警证据列与两张去重/静音表（S1-B）。

迁移只加东西：老告警行的 `trigger_bar_ts`/`dedupe_key` 是 NULL，
不会被回填成一个猜出来的 bar 或键。
"""

from __future__ import annotations

from sams.storage.db import Database
from sams.storage.migrate import apply_migrations


def fresh(tmp_path) -> Database:
    db = Database.open(str(tmp_path / "x.duckdb"))
    apply_migrations(db)
    return db


def test_alerts_gains_the_evidence_columns(tmp_path):
    db = fresh(tmp_path)
    assert {"trigger_bar_ts", "dedupe_key"} <= db.columns("alerts")


def test_states_and_mutes_tables_exist(tmp_path):
    db = fresh(tmp_path)
    names = db.table_names()
    assert "alert_states" in names
    assert "alert_mutes" in names


def test_state_primary_key_covers_the_whole_dedupe_key(tmp_path):
    db = fresh(tmp_path)
    rows = db.query(
        "SELECT constraint_column_names FROM duckdb_constraints()"
        " WHERE table_name = 'alert_states' AND constraint_type = 'PRIMARY KEY'"
    ).to_dicts()
    assert rows
    columns = set(rows[0]["constraint_column_names"])
    assert columns == {"user_id", "rule_id", "market", "code", "account_key"}
