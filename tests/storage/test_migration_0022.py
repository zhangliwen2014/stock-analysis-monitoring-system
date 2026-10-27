"""0022：自选的「移除」改成折起（D169）。

这一份只钉两件事：那一列真的在；**升级前就存在的行在新列上是 NULL**——
一次软件升级不该把任何人已经写好的自选凭空折起来，那一屏看起来像「我的票丢了」。
"""

from __future__ import annotations

import shutil

from sams.storage.db import Database
from sams.storage.migrate import MIGRATIONS_DIR, apply_migrations


def fresh(tmp_path) -> Database:
    db = Database.open(str(tmp_path / "x.duckdb"))
    apply_migrations(db)
    return db


def test_watchlists_gains_the_removed_at_column(tmp_path):
    db = fresh(tmp_path)
    assert "removed_at" in db.columns("watchlists")


def test_rows_that_exist_before_the_upgrade_are_not_hidden(tmp_path):
    """先只应用 0001–0021，写一行自选，再补上 0022：那一行必须还是「看得见」的状态。"""
    partial = tmp_path / "migrations"
    partial.mkdir()
    for path in MIGRATIONS_DIR.glob("*.sql"):
        if path.name < "0022":
            shutil.copy(path, partial / path.name)
    db = Database.open(str(tmp_path / "old.duckdb"))
    apply_migrations(db, partial)
    db.execute(
        "INSERT INTO watchlists (user_id, market, code, note)"
        " VALUES ('u1', 'A', '600519', '旧行')"
    )

    shutil.copy(MIGRATIONS_DIR / "0022_watchlist_hidden.sql", partial)
    assert apply_migrations(db, partial) == ["0022_watchlist_hidden.sql"]

    row = db.query("SELECT note, removed_at FROM watchlists WHERE user_id = 'u1'").to_dicts()[0]
    assert row["note"] == "旧行"
    assert row["removed_at"] is None, "升级不许把已有的自选折起来"


def test_reapplying_is_a_noop(tmp_path):
    db = fresh(tmp_path)
    assert apply_migrations(db) == []
