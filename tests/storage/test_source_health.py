"""源健康落库：验证「换一个进程也还能看到上一次的失败」。"""

from __future__ import annotations

from datetime import date

from sams.core.sources import SourceRecord
from sams.storage.db import Database
from sams.storage.repos.source_health import SourceHealthRepo

RECORD = SourceRecord(
    name="akshare",
    markets=("A",),
    ok=False,
    last_error="RateLimited: 429",
    last_ok_at=date(2026, 10, 16),
    consecutive_failures=2,
)


def test_roundtrip_keeps_every_field(db):
    repo = SourceHealthRepo(db)
    repo.save(RECORD)
    assert repo.load()["akshare"] == RECORD


def test_saving_the_same_source_twice_overwrites(db):
    repo = SourceHealthRepo(db)
    repo.save(RECORD)
    repo.save(SourceRecord(name="akshare", markets=("A",), ok=True, last_ok_at=date(2026, 10, 17)))
    loaded = repo.load()
    assert len(loaded) == 1 and loaded["akshare"].ok and loaded["akshare"].last_error is None


def test_unmigrated_database_loads_empty(tmp_path):
    fresh = Database.open(tmp_path / "fresh.duckdb")
    try:
        assert SourceHealthRepo(fresh).load() == {}
    finally:
        fresh.close()
