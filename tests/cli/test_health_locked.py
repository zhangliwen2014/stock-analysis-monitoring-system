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
        assert "http://127.0.0.1:8765/healthz" in text
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


def test_a_stale_beat_from_a_live_pid_says_a_long_job_not_a_dead_loop(tmp_path, capsys) -> None:
    """那一拍过期了，但写它的进程还在：这说的是「某一趟比阈值长」，不是「调度器停了」。

    本机实测（2026-11-02）：A 股基本面清扫一趟跑几分钟，`_beat()` 只在轮首与每个 job
    之前打，所以单趟超阈值的那一段一定读成过期——健康页于是常年 `degraded`。
    把「可能死了」印在这种时候，看的人学到的第一件事就是别再看这页。
    """
    db = tmp_path / "sams.duckdb"
    write_beat(
        beat_path(db),
        pid=os.getpid(),
        beat_at=datetime.now(UTC) - timedelta(seconds=HEARTBEAT_STALE_S + 60),
    )
    capsys.readouterr()

    code = show_locked(DatabaseLocked(str(db), detail="Conflicting lock"))

    text = capsys.readouterr().out
    assert code == EXIT_LOCKED
    assert "还在" in text and "一趟" in text
    assert "可能已经卡住" not in text


def test_a_stale_beat_from_a_dead_pid_says_the_holder_is_gone(tmp_path, capsys) -> None:
    """写那一拍的进程已经不在了，而锁还没释放：这才是需要人去看的现场。

    单写者模型下这条判据是硬的——只有真没人持锁才可能有第二份写连接，所以拍上的 pid
    死了就等于没有别人在写这一份库（D276）。这里用一个刚退出的真子进程，不留猜测。
    """
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    db = tmp_path / "sams.duckdb"
    write_beat(
        beat_path(db),
        pid=dead.pid,
        beat_at=datetime.now(UTC) - timedelta(seconds=HEARTBEAT_STALE_S + 60),
    )
    capsys.readouterr()

    code = show_locked(DatabaseLocked(str(db), detail="Conflicting lock"))

    text = capsys.readouterr().out
    assert code == EXIT_LOCKED
    assert "已经不在了" in text
    assert "还在写" not in text


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


def test_a_wildcard_bind_is_advertised_as_loopback(tmp_path, capsys, settings) -> None:
    """`0.0.0.0` 不是可访问的主机名：屏幕是这份配置唯一的展示处，
    它不该把一行抄进浏览器才报错的地址印出来。"""
    from dataclasses import replace

    from sams.core.config import ServerCfg

    wide = replace(settings, server=ServerCfg(host="0.0.0.0", allow_lan=True))
    capsys.readouterr()

    code = show_locked(DatabaseLocked(str(tmp_path / "sams.duckdb")), settings=wide)

    text = capsys.readouterr().out
    assert code == EXIT_LOCKED
    assert "http://127.0.0.1:8765/healthz" in text
    assert "0.0.0.0" not in text


def test_an_ipv6_bind_gets_brackets_in_the_url(tmp_path, capsys, settings) -> None:
    """IPv6 地址不带方括号时，冒号会被当成端口分隔符，URL 根本解析不了。"""
    from dataclasses import replace

    from sams.core.config import ServerCfg

    v6 = replace(settings, server=ServerCfg(host="::1", allow_lan=True))
    capsys.readouterr()

    code = show_locked(DatabaseLocked(str(tmp_path / "sams.duckdb")), settings=v6)

    assert code == EXIT_LOCKED
    assert "http://[::1]:8765/healthz" in capsys.readouterr().out
