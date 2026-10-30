"""0029：方案带说明（D220）。

只验形状与老库不丢行；说明的校验与页面行为在各自的用例里。
"""

from __future__ import annotations

import shutil

from sams.storage.db import Database
from sams.storage.migrate import MIGRATIONS_DIR, apply_migrations


def test_0029_adds_a_non_null_description_defaulting_to_empty(db):
    assert "description" in db.columns("screener_presets")
    rows = db.query(
        "SELECT column_name, is_nullable, column_default FROM information_schema.columns"
        " WHERE table_name = 'screener_presets' AND column_name = 'description'"
    ).to_dicts()
    assert rows and rows[0]["is_nullable"] == "NO"
    assert rows[0]["column_default"] == "''"


def test_0029_keeps_existing_presets_with_an_empty_description(tmp_path):
    legacy_dir = tmp_path / "migrations"
    legacy_dir.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name < "0029":
            shutil.copy(path, legacy_dir / path.name)

    database = Database.open(str(tmp_path / "legacy.duckdb"))
    try:
        apply_migrations(database, legacy_dir)
        database.execute(
            """
            INSERT INTO users (id, username, pwd_hash, created_at)
            VALUES ('u1', 'alice', 'x', TIMESTAMP '2026-10-01 00:00:00')
            """
        )
        database.execute(
            """
            INSERT INTO screener_presets
                (id, user_id, name, market, conditions_json, created_at, updated_at)
            VALUES ('p1', 'u1', '便宜', 'A',
                    '[{"field": "pr_now", "op": "<=", "value": 0.5}]',
                    TIMESTAMP '2026-10-01 00:00:00', TIMESTAMP '2026-10-01 00:00:00')
            """
        )
        applied = apply_migrations(database)
        assert "0029_screener_preset_description.sql" in applied

        row = database.query(
            "SELECT name, description FROM screener_presets WHERE id = 'p1'"
        ).to_dicts()[0]
        assert row["name"] == "便宜"
        assert row["description"] == ""
    finally:
        database.close()
