"""0016：`theses` 上的三项基线（spec §9.3 / D96）。

形状照 `tests/storage/test_migration_0015.py` 那份断言：逐列、可空、旧行读回来是 None。
这条迁移只加列，不动任何已有值。
"""

from __future__ import annotations

from sams.storage.db import Database
from sams.storage.migrate import apply_migrations


def fresh(tmp_path) -> Database:
    """迁移文件必须在第一次构建库之前就存在于磁盘：apply 时才读目录算校验和。"""
    db = Database.open(str(tmp_path / "x.duckdb"))
    apply_migrations(db)
    return db


def test_theses_gains_the_three_baseline_columns(tmp_path):
    db = fresh(tmp_path)
    assert {"baseline_score", "baseline_items", "baseline_on"} <= db.columns("theses")
    row = db.query("SELECT baseline_score, baseline_items, baseline_on FROM theses LIMIT 0")
    assert row.columns == ["baseline_score", "baseline_items", "baseline_on"]


def test_applying_migrations_twice_adds_nothing(tmp_path):
    """加一条迁移不能弄脏校验和：`sams db migrate` 在真库上重跑必须是幂等 no-op。"""
    db = fresh(tmp_path)
    before = db.query("SELECT count(*) AS n FROM schema_migrations").to_dicts()[0]["n"]
    apply_migrations(db)
    after = db.query("SELECT count(*) AS n FROM schema_migrations").to_dicts()[0]["n"]
    assert before == after == 16


def test_an_old_row_reads_back_with_no_baseline(tmp_path):
    """0013 之前建的那条论没有基线，读回来必须是 None 而不是 0 分。

    0 分是「这项最差」，None 是「没比过」——把后者读成前者，`thesis.drift` 会在一条
    从没复检过的论上永远报「掉到底了」。
    """
    db = fresh(tmp_path)
    db.execute(
        "INSERT INTO theses (id, user_id, market, code, style, status) VALUES"
        " ('t1', 'u1', 'A', '601088', 'value', 'draft')"
    )
    row = db.query("SELECT baseline_score, baseline_items, baseline_on FROM theses").to_dicts()[0]
    assert row["baseline_score"] is None
    assert row["baseline_items"] is None
    assert row["baseline_on"] is None
