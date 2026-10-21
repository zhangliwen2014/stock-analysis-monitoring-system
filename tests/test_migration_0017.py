"""0017：`rules.params_on`——那一屏参数属于哪一天的价空间（spec §10.2 的除权换算要它当锚）。

形状照 `tests/test_migration_0016.py`：只加一列、可空、旧行读回来是 None。

「重跑一遍不多不少」那一枚不在这里重复：它钉的是整套迁移的幂等，与本条加了什么无关，
`test_migration_0016.py` 里那一份已经把条数从迁移目录推出来了。
"""

from __future__ import annotations

from sams.storage.db import Database
from sams.storage.migrate import apply_migrations


def fresh(tmp_path) -> Database:
    """迁移文件必须在第一次构建库之前就存在于磁盘：apply 时才读目录算校验和。"""
    db = Database.open(str(tmp_path / "x.duckdb"))
    apply_migrations(db)
    return db


def test_rules_gains_the_params_anchor(tmp_path):
    db = fresh(tmp_path)
    assert "params_on" in db.columns("rules")
    row = db.query("SELECT params_on FROM rules LIMIT 0")
    assert row.columns == ["params_on"]


def test_an_old_row_reads_back_without_an_anchor(tmp_path):
    """0017 之前建的那条规则没有日期戳：读回来必须是 None，不是今天。

    「今天填的」在除权换算里是一个能让那条线免搬的结论。把「不知道」读成「今天」，
    等于替这条规则做了一个它没同意过的决定。
    """
    db = fresh(tmp_path)
    db.execute(
        "INSERT INTO rules (id, user_id, name, enabled, target_type, target_ref, kind,"
        " condition_kind, params_json, filters_json, cooldown_min, severity, channel_ids)"
        " VALUES ('r1', 'u1', '跌破 10 元', true, 'symbol', 'A/601088', 'stop.fixed',"
        " 'template', '{}', '{}', 30, 'strong', '[]')"
    )
    assert db.query("SELECT params_on FROM rules").to_dicts()[0]["params_on"] is None
