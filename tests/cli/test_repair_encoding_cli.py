"""`sams db repair-encoding`：默认干跑列清单，--apply 才写回（R3-26/D360）。"""

from __future__ import annotations

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
"""


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "settings.toml"
    path.write_text(CONFIG.format(db_path=tmp_path / "sams.duckdb"), encoding="utf-8")
    monkeypatch.setenv("SAMS_CONFIG", str(path))
    return path


def broken(text: str) -> str:
    return text.encode("utf-8").decode("latin-1")


def seed(db_path):
    database = Database.open(db_path)
    try:
        database.execute(
            "INSERT INTO watchlists (user_id, market, code, note) VALUES (?, ?, ?, ?)",
            ["u1", "A", "600519", broken("验收-混排")],
        )
        database.execute(
            "INSERT INTO thesis_events (id, thesis_id, to_status, actor_id, note)"
            " VALUES ('ev1', 't1', 'open', 'u1', ?)",
            [broken("现场验证")],
        )
    finally:
        database.close()


def read_note(db_path):
    database = Database.open(db_path)
    try:
        return database.scalar("SELECT note FROM watchlists WHERE code = '600519'")
    finally:
        database.close()


def test_dry_run_lists_candidates_and_writes_nothing(config_path, capsys):
    assert cli.run(["db", "migrate"]) == 0
    capsys.readouterr()
    db_path = config_path.parent / "sams.duckdb"
    seed(db_path)

    assert cli.run(["db", "repair-encoding"]) == 0
    out = capsys.readouterr().out
    assert "watchlists.note u1/A/600519" in out
    assert "验收-混排" in out
    assert "thesis_events.note ev1" in out
    assert "预计修复 2 处" in out
    assert read_note(db_path) == broken("验收-混排")


def test_apply_writes_back_and_the_second_run_says_there_is_nothing_left(config_path, capsys):
    assert cli.run(["db", "migrate"]) == 0
    capsys.readouterr()
    db_path = config_path.parent / "sams.duckdb"
    seed(db_path)

    assert cli.run(["db", "repair-encoding", "--apply"]) == 0
    out = capsys.readouterr().out
    assert "已修复 2 处" in out
    assert read_note(db_path) == "验收-混排"

    assert cli.run(["db", "repair-encoding", "--apply"]) == 0
    assert "没有可修复的乱码" in capsys.readouterr().out
