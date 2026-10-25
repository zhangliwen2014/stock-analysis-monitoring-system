"""0020：批注、闭环与审计的存储面（M4-B spec §3）。

这一份钉三件事：新表/新列真的在；策略默认值真的是 `on`；**旧行不被改写**——
迁移只加列，一条 M3 时代的 `handled` 在升级后仍然是 `action_kind = NULL`，
复盘里它会单列「旧版」，而不是被回填成一个我们猜的动作。
"""

from __future__ import annotations

import shutil

from sams.storage.db import Database
from sams.storage.migrate import MIGRATIONS_DIR, apply_migrations


def fresh(tmp_path) -> Database:
    db = Database.open(str(tmp_path / "x.duckdb"))
    apply_migrations(db)
    return db


def test_new_tables_exist(tmp_path):
    db = fresh(tmp_path)
    names = db.table_names()
    for table in ("alert_events", "annotation_attachments", "app_settings", "admin_access_log"):
        assert table in names, table


def test_required_indexes_exist(tmp_path):
    db = fresh(tmp_path)
    indexes = {
        row["index_name"]
        for row in db.query("SELECT index_name FROM duckdb_indexes()").to_dicts()
    }
    assert {
        "idx_alert_events_alert",
        "idx_annotations_target",
        "idx_annotations_user",
        "idx_annotation_attachments_annotation",
        "idx_admin_access_admin",
        "idx_admin_access_annotation",
    } <= indexes


def test_alerts_gains_the_closure_columns(tmp_path):
    db = fresh(tmp_path)
    assert {
        "acknowledged_at",
        "action_kind",
        "action_ref",
        "dismiss_code",
        "thesis_id",
        "deferred_until",
    } <= db.columns("alerts")


def test_annotations_gain_snapshot_and_tombstone_columns(tmp_path):
    db = fresh(tmp_path)
    assert {"target_label", "deleted_at", "deleted_by", "purged_at"} <= db.columns("annotations")
    assert "meta_json" in db.columns("annotation_revisions")
    assert "context_json" in db.columns("notify_log")


def test_team_sharing_defaults_to_on(tmp_path):
    db = fresh(tmp_path)
    value = db.scalar("SELECT value FROM app_settings WHERE key = 'annotations.team_sharing'")
    assert value == "on"


def test_an_old_handled_row_is_not_rewritten(tmp_path):
    """先只应用 0001–0019，写一条旧式 handled，再应用 0020。"""
    partial = tmp_path / "migrations"
    partial.mkdir()
    for path in MIGRATIONS_DIR.glob("*.sql"):
        if path.name < "0020":
            shutil.copy(path, partial / path.name)
    db = Database.open(str(tmp_path / "old.duckdb"))
    apply_migrations(db, partial)
    db.execute(
        "INSERT INTO alerts (id, user_id, rule_id, account_id, market, code, stage,"
        " triggered_at, payload_json, status, handled_at, handle_note, dismiss_reason,"
        " severity) VALUES ('a1', 'u1', 'r1', NULL, 'A', '601088', '入池',"
        " TIMESTAMPTZ '2026-10-20 14:30:00+00', '{}', 'handled',"
        " TIMESTAMPTZ '2026-10-20 14:31:00+00', '已挂单', NULL, 'mid')"
    )

    shutil.copy(MIGRATIONS_DIR / "0020_annotations_review.sql", partial)
    assert apply_migrations(db, partial) == ["0020_annotations_review.sql"]

    row = db.query(
        "SELECT status, handled_at, action_kind, acknowledged_at, dismiss_code"
        " FROM alerts WHERE id = 'a1'"
    ).to_dicts()[0]
    assert row["status"] == "handled"
    assert row["handled_at"] is not None
    assert row["action_kind"] is None
    assert row["acknowledged_at"] is None
    assert row["dismiss_code"] is None


def test_old_annotation_rows_get_empty_defaults(tmp_path):
    """M3 的批注与修订没有新列；0020 回填空值，但不改写正文与标签。"""
    partial = tmp_path / "migrations"
    partial.mkdir()
    for path in MIGRATIONS_DIR.glob("*.sql"):
        if path.name < "0020":
            shutil.copy(path, partial / path.name)
    db = Database.open(str(tmp_path / "old.duckdb"))
    apply_migrations(db, partial)
    db.execute(
        "INSERT INTO annotations (id, user_id, target_type, target_ref, body_md)"
        " VALUES ('n1', 'u1', 'symbol', 'A:600519', '旧正文')"
    )
    db.execute(
        "INSERT INTO annotation_revisions (id, annotation_id, user_id, before_md, after_md)"
        " VALUES ('v1', 'n1', 'u1', '改前', '改后')"
    )

    shutil.copy(MIGRATIONS_DIR / "0020_annotations_review.sql", partial)
    assert apply_migrations(db, partial) == ["0020_annotations_review.sql"]

    annotation = db.query(
        "SELECT target_label, body_md, tags_json, pinned FROM annotations WHERE id = 'n1'"
    ).to_dicts()[0]
    assert annotation["target_label"] == ""
    assert annotation["body_md"] == "旧正文"
    assert annotation["tags_json"] == "[]"
    assert annotation["pinned"] is False

    revision = db.query(
        "SELECT meta_json, before_md, after_md"
        " FROM annotation_revisions WHERE id = 'v1'"
    ).to_dicts()[0]
    assert revision["meta_json"] == "{}"
    assert revision["before_md"] == "改前"
    assert revision["after_md"] == "改后"


def test_new_rows_get_the_column_defaults(tmp_path):
    """新库写入不给新列也能落空值：默认值真的生效，而不是只靠回填。"""
    db = fresh(tmp_path)
    db.execute(
        "INSERT INTO annotations (id, user_id, target_type, target_ref, body_md)"
        " VALUES ('n1', 'u1', 'symbol', 'A:600519', '正文')"
    )
    db.execute(
        "INSERT INTO annotation_revisions (id, annotation_id, user_id, before_md, after_md)"
        " VALUES ('v1', 'n1', 'u1', '改前', '改后')"
    )

    assert db.scalar("SELECT target_label FROM annotations WHERE id = 'n1'") == ""
    assert db.scalar("SELECT meta_json FROM annotation_revisions WHERE id = 'v1'") == "{}"


def test_reapplying_is_a_noop(tmp_path):
    db = fresh(tmp_path)
    assert apply_migrations(db) == []
