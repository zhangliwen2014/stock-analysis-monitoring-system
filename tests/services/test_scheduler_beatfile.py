"""库旁那一拍（D111）：常驻调度器在库被自己握着时，外部读者还能看见的只有这个文件。

它要回答的只有两件事——「谁在拿库」与「最后一拍是什么时候」。所以形状小到可以一眼
看完，损坏或半写就走 `None`：一个读不懂的文件不该让健康页编一个年龄出来。
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime

from sams.services.scheduler.beatfile import (
    BeatFile,
    beat_path,
    pid_is_gone,
    read_beat,
    write_beat,
)

MOMENT = datetime(2024, 10, 25, 23, 0, tzinfo=UTC)


def test_beat_file_lives_next_to_the_database(tmp_path) -> None:
    path = beat_path(tmp_path / "sams.duckdb")
    assert path is not None
    assert path.parent == tmp_path
    assert path.name == "sams.duckdb.heartbeat.json"


def test_memory_database_has_no_beat_file() -> None:
    assert beat_path(":memory:") is None


def test_roundtrip(tmp_path) -> None:
    path = beat_path(tmp_path / "sams.duckdb")
    assert path is not None
    write_beat(path, pid=4321, beat_at=MOMENT)
    assert read_beat(path) == BeatFile(pid=4321, beat_at=MOMENT)


def test_corrupt_file_reads_as_unknown(tmp_path) -> None:
    path = beat_path(tmp_path / "sams.duckdb")
    assert path is not None
    path.write_text("{ 这不是 JSON", encoding="utf-8")
    assert read_beat(path) is None


def test_write_is_atomic_and_leaves_no_temp_behind(tmp_path) -> None:
    """写到一半被读到的文件会说「心跳在几分钟前」——那是编出来的年龄。

    同目录临时文件 + `os.replace`：读者要么看见上一拍，要么看见这一拍。
    """
    path = beat_path(tmp_path / "sams.duckdb")
    assert path is not None
    write_beat(path, pid=1, beat_at=MOMENT)
    write_beat(path, pid=2, beat_at=MOMENT)
    assert read_beat(path).pid == 2
    assert [p.name for p in tmp_path.iterdir()] == [path.name]


def test_naive_timestamp_reads_as_unknown(tmp_path) -> None:
    """没有时区的时刻加上「距今多久」只能靠猜：猜出来的是一个看着正常的错数。"""
    path = beat_path(tmp_path / "sams.duckdb")
    assert path is not None
    path.write_text(
        '{"pid": 1, "beat_at": "2024-10-25T23:00:00"}', encoding="utf-8"
    )
    assert read_beat(path) is None
    assert os.path.exists(path)


def test_a_reaped_child_reads_as_gone(tmp_path) -> None:
    """「那个进程不在了」是一个可以问出来的事实，不需要猜心跳的年龄。

    单写者模型给了这条判据分量：能开写连接的人只有一个，所以拍上那个 pid 死了
    就等于没有别人在写这一份库（D276）。这里用一个刚退出的真子进程，不留 pid 猜测。
    """
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    assert pid_is_gone(dead.pid) is True


def test_this_process_reads_as_alive() -> None:
    assert pid_is_gone(os.getpid()) is False


def test_an_unusable_pid_reads_as_unknown() -> None:
    """判不了要说「不知道」，不是「死了」也不是「活着」。

    超出系统的 pid 连信号都发不出去：那一刻既没证明它在，也没证明它不在。
    """
    assert pid_is_gone(2**31) is None
