"""库旁那一拍（D111）：常驻调度器在库被自己握着时，外部读者还能看见的只有这个文件。

它要回答的只有两件事——「谁在拿库」与「最后一拍是什么时候」。所以形状小到可以一眼
看完，损坏或半写就走 `None`：一个读不懂的文件不该让健康页编一个年龄出来。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

from sams.services.scheduler.beatfile import BeatFile, beat_path, read_beat, write_beat

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
