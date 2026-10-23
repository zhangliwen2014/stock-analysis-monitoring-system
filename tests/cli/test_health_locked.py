"""库被另一个进程握着时，`sams health` 与其它命令各自该说什么、以什么码退出（D111）。

这一枚走真进程：DuckDB 的冲突锁是文件锁，进程内的替身证明不了「第二个进程打不开」。
DuckDB 1.5.6 的实测是另一个进程**连只读连接也打不开**，所以健康页没有「绕开锁读库」
这条路，它只能改读库旁那一拍——这也是 `[调度]` 那一节在锁住时唯一能回答的事。

3 与 1 是两个不同的结论：1 是「读到了，而且有故障」；3 是「读不到，所以不知道」。
把两者混成一个码，看门狗就只能把「正在干活」与「已经死了」看成同一件事。
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta

from sams.cli import run
from sams.cli.common import EXIT_LOCKED
from sams.cli.health import show_locked
from sams.core.errors import DatabaseLocked
from sams.services.scheduler.beatfile import beat_path, write_beat
from sams.services.scheduler.heartbeat import HEARTBEAT_STALE_S

CONFIG = """
[storage]
db_path = "{db}"

[general]
secret_key = "throwaway-key-for-the-lock-test"
report_currency = "CNY"
scheduler_tick_s = 60

[markets.A]
tz = "Asia/Shanghai"
currency = "CNY"
post_close_min = 30
sources = ["akshare", "eastmoney"]

[markets.HK]
tz = "Asia/Hong_Kong"
currency = "HKD"
post_close_min = 30
sources = ["yfinance", "tencent"]

[markets.US]
tz = "America/New_York"
currency = "USD"
post_close_min = 30
sources = ["yfinance", "tencent"]
"""

HOLDER = (
    "import duckdb, sys, time\n"
    "conn = duckdb.connect(sys.argv[1])\n"
    "print('HOLDER_OPEN', flush=True)\n"
    "time.sleep(30)\n"
)


def _config(tmp_path, db) -> str:
    path = tmp_path / "settings.toml"
    path.write_text(CONFIG.format(db=db), encoding="utf-8")
    return str(path)


def _holder(db) -> subprocess.Popen:
    process = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(db)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "HOLDER_OPEN", "占库进程没起来"
    return process


def test_health_and_the_other_commands_exit_3_while_a_writer_holds_the_database(
    tmp_path, capsys
) -> None:
    db = tmp_path / "sams.duckdb"
    config = _config(tmp_path, db)
    assert run(["--config", config, "db", "migrate"]) == 0

    holder = _holder(db)
    try:
        write_beat(beat_path(db), pid=holder.pid, beat_at=datetime.now(UTC))
        capsys.readouterr()

        code = run(["--config", config, "health"])
        text = capsys.readouterr().out
        assert code == EXIT_LOCKED
        assert f"PID {holder.pid}" in text
        assert "心跳" in text
        assert "退出码 3" in text

        other = run(["--config", config, "user", "list"])
        captured = capsys.readouterr()
        assert other == EXIT_LOCKED
        assert "数据库被占用" in captured.err
        # 「在干活」不是「配置写错了」：那一句会把人送去改一个没错的文件
        assert "配置错误" not in captured.err
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_a_stale_beat_file_says_the_holder_may_be_stuck(tmp_path, capsys) -> None:
    """库仍然被锁着，但那一拍已经很旧：占着它的人可能卡住了，锁不会自己释放。
    健康的那个进程与卡死的那个进程在「库打不开」这一件事上完全同形，心跳文件是唯一的分界。
    """
    db = tmp_path / "sams.duckdb"
    write_beat(
        beat_path(db),
        pid=os.getpid(),
        beat_at=datetime.now(UTC) - timedelta(seconds=HEARTBEAT_STALE_S + 60),
    )
    capsys.readouterr()

    code = show_locked(DatabaseLocked(str(db), detail="Conflicting lock"))

    assert code == EXIT_LOCKED
    assert "可能已经卡住" in capsys.readouterr().out


def test_without_a_beat_file_the_screen_does_not_pretend_to_know(tmp_path, capsys) -> None:
    """没有文件可能是「刚启动」也可能是「占库的根本不是调度器」（一条长采集）。
    两种都不该被写成「它死了」，也不该被写成「它在跑」。
    """
    capsys.readouterr()

    code = show_locked(DatabaseLocked(str(tmp_path / "sams.duckdb")))

    text = capsys.readouterr().out
    assert code == EXIT_LOCKED
    assert "没有心跳文件" in text
    assert "可能已经停了" not in text
