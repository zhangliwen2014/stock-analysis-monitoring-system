"""0015：通道的名字与错误、用户的免打扰、notify_log 的四值结果。

这条迁移的三处改动各挡一种「不会报错的错法」，所以逐列断言，不接受「表能建」。
"""

from __future__ import annotations

from sams.storage.db import Database
from sams.storage.migrate import apply_migrations


def fresh(tmp_path) -> Database:
    """迁移文件必须在第一次构建库之前就存在于磁盘：apply 时才读目录算校验和。"""
    db = Database.open(str(tmp_path / "x.duckdb"))
    apply_migrations(db)
    return db


def test_channels_gains_name_last_error_and_created_at(tmp_path):
    db = fresh(tmp_path)
    cols = db.columns("channels")
    assert {"name", "last_error", "last_error_at", "created_at"} <= cols
    row = db.query(
        "SELECT enabled, is_default, last_error, last_error_at, created_at FROM channels LIMIT 0"
    )
    assert row.height == 0  # 只是证明列名拼对了


def test_notify_log_outcome_replaces_success(tmp_path):
    db = fresh(tmp_path)
    cols = db.columns("notify_log")
    assert "outcome" in cols
    assert "success" not in cols, "success 留着就有「两种真相」：一个说成败、一个说四态"
    # alert_id 仍然 NOT NULL：no_channel/suppressed 也都属于一条已经存在的 alert
    assert "alert_id" in cols
    got = db.query("SELECT coalesce(outcome, 'failed') AS o FROM notify_log LIMIT 0")
    assert got.columns == ["o"]


def test_users_gains_the_three_dnd_columns(tmp_path):
    db = fresh(tmp_path)
    assert {"dnd_start", "dnd_end", "dnd_tz"} <= db.columns("users")


def test_alerts_gains_severity(tmp_path):
    """告警要按级别路由，而级别属于那一条告警、不属于规则：规则改了级别，
    历史里那一条仍然是当时那一条。"""
    db = fresh(tmp_path)
    assert "severity" in db.columns("alerts")


def test_alert_rule_id_is_optional(tmp_path):
    """插入一行 `rule_id=None` 的告警应当成功。

    查 `information_schema.columns.is_nullable` 只证明那句 DDL 跑过，不证明这条路径能用；
    能用的证明是一次真的 INSERT。手工投递与 M3-D 的系统告警都没有规则可挂。
    """
    db = fresh(tmp_path)
    db.execute(
        "INSERT INTO alerts (id, user_id, rule_id, market, code, stage, payload_json)"
        " VALUES ('a-1', 'u1', NULL, 'A', '601088', 'pool', '{}')"
    )
    assert db.scalar("SELECT rule_id FROM alerts WHERE id = 'a-1'") is None


def test_reapplying_is_idempotent(tmp_path):
    db = fresh(tmp_path)
    apply_migrations(db)  # 第二次什么都不该发生（ADD COLUMN IF NOT EXISTS）
    assert "outcome" in db.columns("notify_log")
