"""`task_runs` 的读写：周期键是「这个周期跑过没有」的唯一依据（D98）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sams.core.errors import ValidationError
from sams.storage.repos.task_runs import FAILED, OK, RUNNING, TaskRunRepo


class Clock:
    def __init__(self, moment):
        self.moment = moment

    def now_utc(self):
        return self.moment

    def today(self):
        return self.moment.date()

    def advance(self, **kwargs):
        self.moment += timedelta(**kwargs)


def test_start_and_finish_round_trip(db):
    clock = Clock(datetime(2024, 10, 25, 8, 30, tzinfo=UTC))
    repo = TaskRunRepo(db, clock=clock)
    run_id = repo.start("bars", market="A", period_key="2024-10-25")
    running = repo.get(run_id)
    assert running is not None and running.status == RUNNING
    assert running.finished_at is None

    clock.advance(days=0, seconds=5)
    done = repo.finish(run_id, status=OK, rows_affected=42, note="更新 3 只")
    assert done.status == OK
    assert done.rows_affected == 42
    assert done.note == "更新 3 只"
    assert done.finished_at is not None
    assert repo.latest_for_period("bars", "A", "2024-10-25").id == run_id


def test_latest_for_period_does_not_leak_across_periods(db):
    clock = Clock(datetime(2024, 10, 25, 8, 30, tzinfo=UTC))
    repo = TaskRunRepo(db, clock=clock)
    old = repo.start("bars", market="A", period_key="2024-10-24")
    repo.finish(old, status=OK)
    assert repo.latest_for_period("bars", "A", "2024-10-25") is None
    assert repo.latest_for_period("bars", "A", "2024-10-24").id == old


def test_global_job_uses_null_market(db):
    clock = Clock(datetime(2024, 10, 25, 8, 30, tzinfo=UTC))
    repo = TaskRunRepo(db, clock=clock)
    run_id = repo.start("fx", period_key="2024-10-25")
    repo.finish(run_id, status=OK)
    market_run = repo.start("fx", market="A", period_key="2024-10-25")
    repo.finish(market_run, status=OK)
    assert repo.latest_for_period("fx", None, "2024-10-25").id == run_id
    # 市场维度不串：A 的那一行不是「全局那条」的替身
    assert repo.latest_for_period("fx", "A", "2024-10-25").id == market_run


def test_start_rejects_an_empty_job_name(db):
    repo = TaskRunRepo(db)
    with pytest.raises(ValidationError, match="job_name"):
        repo.start("", market="A")


def test_finish_rejects_unknown_status(db):
    repo = TaskRunRepo(db)
    run_id = repo.start("bars", market="A", period_key="2024-10-25")
    with pytest.raises(ValidationError, match="status"):
        repo.finish(run_id, status="done")


def test_recent_is_newest_first_and_sized(db):
    clock = Clock(datetime(2024, 10, 25, 8, 30, tzinfo=UTC))
    repo = TaskRunRepo(db, clock=clock)
    ids = []
    for _ in range(4):
        ids.append(repo.start("bars", market="A", period_key="2024-10-25"))
        repo.finish(ids[-1], status=FAILED, error="boom")
        clock.advance(minutes=1)
    recent = repo.recent("bars", "A", limit=3)
    assert [row.id for row in recent] == list(reversed(ids[1:]))


def test_unfinished_run_is_still_latest(db):
    clock = Clock(datetime(2024, 10, 25, 8, 30, tzinfo=UTC))
    repo = TaskRunRepo(db, clock=clock)
    run_id = repo.start("bars", market="A", period_key="2024-10-25")
    clock.advance(hours=7)
    latest = repo.latest_for_period("bars", "A", "2024-10-25")
    assert latest.id == run_id and latest.status == RUNNING
