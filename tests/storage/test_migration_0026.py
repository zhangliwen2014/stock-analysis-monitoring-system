"""0026：账户的主场市场（D208）。

这条迁移要对**旧行**正确，而不只是在新库上绿：升级前开的户都开在只有 A 股记账的
场景里，它们必须变成 A 股账户——留在 NULL 的话，录入表单的 `home in markets`
判定会把它们悄悄退回字母序第一个市场，症状与「这个功能没生效」一模一样。
"""

from __future__ import annotations

import shutil

from sams.storage.db import Database
from sams.storage.migrate import MIGRATIONS_DIR, apply_migrations


def fresh(tmp_path) -> Database:
    db = Database.open(str(tmp_path / "x.duckdb"))
    apply_migrations(db)
    return db


def test_sim_accounts_gains_the_home_market_column(tmp_path):
    db = fresh(tmp_path)
    assert "home_market" in db.columns("sim_accounts")


def test_an_account_that_exists_before_the_upgrade_becomes_an_a_account(tmp_path):
    partial = tmp_path / "migrations"
    partial.mkdir()
    for path in MIGRATIONS_DIR.glob("*.sql"):
        if path.name < "0026":
            shutil.copy(path, partial / path.name)
    db = Database.open(str(tmp_path / "old.duckdb"))
    apply_migrations(db, partial)
    db.execute(
        "INSERT INTO sim_accounts (id, user_id, name, report_currency)"
        " VALUES ('a1', 'u1', '旧户', 'CNY')"
    )

    shutil.copy(MIGRATIONS_DIR / "0026_account_home_market.sql", partial)
    assert apply_migrations(db, partial) == ["0026_account_home_market.sql"]

    row = db.query("SELECT name, home_market FROM sim_accounts WHERE id = 'a1'").to_dicts()[0]
    assert row["name"] == "旧户"
    assert row["home_market"] == "A", "旧户的事实就是 A 股账户，不是「没选过」"


def test_reapplying_is_a_noop(tmp_path):
    db = fresh(tmp_path)
    assert apply_migrations(db) == []
