"""`sams db prune`：三个开关、dry-run 只报不删、实删与报告一致。"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta

import pytest

import sams.cli as cli
from sams.storage.db import Database

CONFIG = """
[storage]
db_path = "{db_path}"

[general]
secret_key = "test-secret"

[markets.A]
tz = "Asia/Shanghai"
currency = "CNY"
post_close_min = 30
sources = ["akshare"]

[markets.HK]
tz = "Asia/Hong_Kong"
currency = "HKD"
sources = ["yfinance"]
"""


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "settings.toml"
    path.write_text(CONFIG.format(db_path=tmp_path / "sams.duckdb"), encoding="utf-8")
    monkeypatch.setenv("SAMS_CONFIG", str(path))
    return path


def seed(db_path):
    """10 行快照（HK 7 天 + A 3 天）、3 行台账、5 条心跳、一条过期的补数 done 行。"""
    database = Database.open(db_path)
    try:
        today = date.today()
        for offset in range(7):
            day = today - timedelta(days=offset)
            for market, code in (("HK", "00700"), ("A", "600519")):
                if market == "A" and offset >= 3:
                    continue
                database.execute(
                    "INSERT INTO quotes_snapshot (market, code, ts, price, source)"
                    " VALUES (?, ?, ?, 10.0, 'test')",
                    [market, code, datetime(day.year, day.month, day.day, 2, tzinfo=UTC)],
                )
        now = datetime.now(UTC)
        for run_id, days in (("old-1", 200), ("old-2", 100), ("new", 1)):
            database.execute(
                "INSERT INTO task_runs (id, job_name, started_at, status)"
                " VALUES (?, 'bars', ?, 'ok')",
                [run_id, now - timedelta(days=days)],
            )
        for index in range(5):
            beat = now - timedelta(hours=5 - index)
            database.execute(
                "INSERT INTO scheduler_heartbeats (id, started_at, beat_at)"
                " VALUES (?, ?, ?)",
                [f"beat-{index}", beat, beat],
            )
        # 结案 100 天的 done 行：默认 90 天的保留窗该把它收走（终审 重要1）
        database.execute(
            "INSERT INTO symbol_backfills"
            " (id, market, code, tries, status, open_key, created_at, done_at)"
            " VALUES ('done-old', 'A', '600519', 0, 'done', NULL, ?, ?)",
            [now - timedelta(days=100), now - timedelta(days=100)],
        )
    finally:
        database.close()


def open_db(config_path):
    return Database.open(config_path.parent / "sams.duckdb")


def test_prune_dry_run_prints_counts_and_leaves_the_database_untouched(config_path, capsys):
    assert cli.run(["db", "migrate"]) == 0
    capsys.readouterr()
    seed(config_path.parent / "sams.duckdb")

    assert (
        cli.run(
            [
                "db",
                "prune",
                "--dry-run",
                "--quotes-sessions",
                "5",
                "--task-runs-days",
                "90",
                "--heartbeats-keep",
                "2",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "quotes_snapshot: 将删除 2 行" in out
    assert "task_runs: 将删除 2 行" in out
    assert "scheduler_heartbeats: 将删除 3 行" in out
    assert "symbol_backfills: 将删除 1 行" in out
    assert "预计: 8 行" in out

    database = open_db(config_path)
    try:
        assert database.row_count("quotes_snapshot") == 10
        assert database.row_count("scheduler_heartbeats") == 5
        assert database.row_count("symbol_backfills") == 1
        assert database.scalar("SELECT count(*) FROM task_runs WHERE job_name = 'prune'") == 0
    finally:
        database.close()


def test_prune_deletes_and_writes_one_row_per_step(config_path, capsys):
    assert cli.run(["db", "migrate"]) == 0
    capsys.readouterr()
    seed(config_path.parent / "sams.duckdb")

    assert (
        cli.run(
            [
                "db",
                "prune",
                "--task-runs-days",
                "90",
                "--heartbeats-keep",
                "2",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "quotes_snapshot: 删除 2 行" in out
    assert "symbol_backfills: 删除 1 行" in out
    assert "合计: 8 行" in out

    database = open_db(config_path)
    try:
        assert database.row_count("quotes_snapshot") == 8
        assert database.row_count("scheduler_heartbeats") == 2
        assert database.row_count("symbol_backfills") == 0
        rows = database.query(
            "SELECT note, rows_affected FROM task_runs"
            " WHERE job_name = 'prune' ORDER BY rowid"
        ).to_dicts()
        assert [row["note"] for row in rows] == [
            "quotes_snapshot",
            "task_runs",
            "scheduler_heartbeats",
            "symbol_backfills",
        ]
        assert [row["rows_affected"] for row in rows] == [2, 2, 3, 1]
    finally:
        database.close()


@pytest.mark.parametrize(
    "flag",
    ["--quotes-sessions", "--task-runs-days", "--heartbeats-keep", "--backfill-done-days"],
)
def test_zero_retention_is_a_usage_error(config_path, flag):
    """敲错开关是用法错误（退出 2），不是「这次没删东西」（退出 0）。"""
    with pytest.raises(SystemExit) as exc:
        cli.run(["db", "prune", flag, "0"])
    assert exc.value.code == 2


def add_orphan_file(config_path):
    root = config_path.parent / "attachments" / "u1" / "2026" / "01"
    root.mkdir(parents=True)
    path = root / "orphan.png"
    path.write_bytes(b"1234567")
    old = (datetime.now(UTC) - timedelta(days=31)).timestamp()
    os.utime(path, (old, old))
    return path


def test_prune_reports_and_collects_orphan_attachments(config_path, capsys):
    assert cli.run(["db", "migrate"]) == 0
    capsys.readouterr()
    path = add_orphan_file(config_path)

    assert cli.run(["db", "prune", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "attachments_orphan: 将删除 1 个文件" in out
    assert "文件: 将删除 1 个（7 字节）" in out
    assert path.exists()

    assert cli.run(["db", "prune"]) == 0
    out = capsys.readouterr().out
    assert "attachments_orphan: 删除 1 个文件" in out
    assert not path.exists()


@pytest.mark.parametrize(
    "flag", ["--annotations-purged-days", "--attachments-orphan-days"]
)
def test_annotation_retention_is_a_positive_int(config_path, flag):
    with pytest.raises(SystemExit) as exc:
        cli.run(["db", "prune", flag, "0"])
    assert exc.value.code == 2
