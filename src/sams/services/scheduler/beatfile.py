"""库旁那一拍（D111）：常驻调度器握着库时，外部读者还能看见的唯一活证据。

为什么需要它：DuckDB 是单写者模型，`Database.open` 在另一个进程持有读写连接时
连只读连接也会被拒（1.5.6 实测）。`sams health` 因此打不开库，`scheduler_heartbeats`
那张表也一起读不到。把同一拍写成库旁一个小文件，健康页在锁住时改读它：
文件新 = 那个进程还在写；文件旧 = 它可能卡住或已经死了（锁不会自己释放）。

文件只回答「谁」与「什么时候」，不复制任何业务数：多一个字段就多一份需要跟库里
对账的账。写用同目录临时文件 + `os.replace`，读者要么看见上一拍、要么看见这一拍，
不会看见一个写到一半的时间戳。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sams.storage.db import MEMORY

#: 挨着库文件放，名字带上库名：同一个目录里有两个库时，两个心跳不会互相冒充
SUFFIX = ".heartbeat.json"


@dataclass(frozen=True)
class BeatFile:
    pid: int
    beat_at: datetime


def beat_path(db_path: str | Path) -> Path | None:
    """这个库的心跳文件；内存库没有文件可写。"""
    text = str(db_path)
    if text == MEMORY:
        return None
    path = Path(text)
    return path.with_name(path.name + SUFFIX)


def write_beat(path: Path | None, *, pid: int, beat_at: datetime) -> None:
    """原子地写下这一拍。时区必须已经带上：一个裸时刻加上「距今多久」只能靠猜。"""
    if path is None:
        return
    if beat_at.tzinfo is None:
        raise ValueError("beat_at 必须带时区：心跳的年龄不能靠猜本机时区")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"pid": int(pid), "beat_at": beat_at.astimezone(UTC).isoformat()}
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def read_beat(path: Path | None) -> BeatFile | None:
    """读不懂、缺字段、没有时区都返回 `None`：健康页不该为一个坏文件编一个年龄。"""
    if path is None:
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        pid = int(raw["pid"])
        beat_at = datetime.fromisoformat(raw["beat_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if beat_at.tzinfo is None:
        return None
    return BeatFile(pid=pid, beat_at=beat_at)
