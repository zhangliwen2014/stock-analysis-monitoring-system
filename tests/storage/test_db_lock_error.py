"""库被另一个进程持有时，`Database.open` 要抛一个认得出的类型（D111）。

DuckDB 1.5.6 的实测：一个进程以读写方式打开库文件后，**另一个进程连只读连接也打不开**
（`Conflicting lock`）。所以「锁住」不是配置错误，也不是可以绕开的状态：它是
「这一次读不成，但那个进程可能正在干活」。CLI 的退出码要按这件事分开。

类型本身必须从 DuckDB 那句文本里认出 PID：健康页在库打不开时要说出「是谁在拿它」，
而那是唯一一处还留着那个数的地方。
"""

from __future__ import annotations

import duckdb
import pytest

from sams.core.errors import DatabaseLocked, StorageError
from sams.storage.db import Database

LOCK_TEXT = (
    'IO Error: Could not set lock on file "/tmp/x.duckdb": Conflicting lock is held in '
    "/usr/bin/python3.13 (PID 14837) by user liwen.zhang. See also "
    "https://duckdb.org/docs/stable/connect/concurrency"
)


def test_conflicting_lock_becomes_database_locked(monkeypatch, tmp_path) -> None:
    def boom(target):
        raise duckdb.IOException(LOCK_TEXT)

    monkeypatch.setattr("sams.storage.db.duckdb.connect", boom)
    with pytest.raises(DatabaseLocked) as caught:
        Database.open(tmp_path / "x.duckdb")
    assert caught.value.path == str(tmp_path / "x.duckdb")
    assert caught.value.holder_pid == 14837
    assert isinstance(caught.value, StorageError)


def test_other_duckdb_errors_stay_storage_errors(monkeypatch, tmp_path) -> None:
    """不是锁的那些（权限、损坏）不能冒充「被占用」：那个码会让人去等一个不存在的进程。"""

    def boom(target):
        raise duckdb.IOException("IO Error: Cannot open file: Permission denied")

    monkeypatch.setattr("sams.storage.db.duckdb.connect", boom)
    with pytest.raises(StorageError) as caught:
        Database.open(tmp_path / "x.duckdb")
    assert not isinstance(caught.value, DatabaseLocked)
