"""清理服务：保留边界、dry-run 与实删同一份计数、每一步留一行 task_runs。"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest
from support import FrozenClock

from sams.core.errors import ValidationError
from sams.services.prune import PruneService
from sams.storage.repos.quotes import QuoteRepo
from sams.storage.repos.task_runs import HeartbeatRepo, TaskRunRepo

NOW = datetime(2026, 10, 23, 4, 0, tzinfo=UTC)


def make(db, clock=None):
    clock = clock or FrozenClock(NOW)
    return PruneService(
        db,
        quotes=QuoteRepo(db),
        task_runs=TaskRunRepo(db, clock=clock),
        heartbeats=HeartbeatRepo(db, clock=clock),
        clock=clock,
    )


def seed_quotes(db):
    # HK 有 7 天、A 只有 3 天：保留 5 天时只有 HK 最旧的两天该走
    for offset in range(7):
        day = date(2026, 10, 1) + timedelta(days=offset)
        for market, code in (("HK", "00700"), ("A", "600519")):
            if market == "A" and offset >= 3:
                continue
            db.execute(
                "INSERT INTO quotes_snapshot (market, code, ts, price, source)"
                " VALUES (?, ?, ?, 10.0, 'test')",
                [market, code, datetime.combine(day, time(2, 0), tzinfo=UTC)],
            )


def seed_task_runs(db):
    for run_id, days in (("old-1", 200), ("old-2", 100), ("new", 1)):
        db.execute(
            "INSERT INTO task_runs (id, job_name, started_at, status)"
            " VALUES (?, 'bars', ?, 'ok')",
            [run_id, NOW - timedelta(days=days)],
        )


def seed_heartbeats(db):
    clock = FrozenClock(NOW - timedelta(hours=10))
    repo = HeartbeatRepo(db, clock=clock)
    for _ in range(5):
        repo.start(note="tick=60s")
        clock.advance(minutes=1)


def test_run_deletes_by_the_three_retention_windows(db):
    seed_quotes(db)
    seed_task_runs(db)
    seed_heartbeats(db)
    report = make(db).run(quotes_sessions=5, task_runs_days=90, heartbeats_keep=2)
    assert report.dry_run is False
    assert [(step.name, step.rows) for step in report.steps] == [
        ("quotes_snapshot", 2),
        ("task_runs", 2),
        ("scheduler_heartbeats", 3),
    ]
    assert report.total == 7
    assert db.scalar("SELECT count(*) FROM quotes_snapshot WHERE market = 'A'") == 3
    assert db.scalar("SELECT count(*) FROM quotes_snapshot WHERE market = 'HK'") == 5
    left = db.query("SELECT id FROM task_runs WHERE job_name = 'bars'").to_dicts()
    assert [row["id"] for row in left] == ["new"]
    assert db.row_count("scheduler_heartbeats") == 2


def test_dry_run_counts_are_the_same_numbers_and_change_nothing(db):
    seed_quotes(db)
    seed_task_runs(db)
    seed_heartbeats(db)
    service = make(db)
    dry = service.run(quotes_sessions=5, task_runs_days=90, heartbeats_keep=2, dry_run=True)
    assert [step.rows for step in dry.steps] == [2, 2, 3]
    assert db.row_count("quotes_snapshot") == 10, "dry-run 一行都不删"
    assert db.row_count("task_runs") == 3
    assert db.row_count("scheduler_heartbeats") == 5
    real = service.run(quotes_sessions=5, task_runs_days=90, heartbeats_keep=2)
    assert [step.rows for step in real.steps] == [step.rows for step in dry.steps]


def test_each_step_leaves_one_prune_row(db):
    seed_quotes(db)
    seed_task_runs(db)
    seed_heartbeats(db)
    make(db).run(quotes_sessions=5, task_runs_days=90, heartbeats_keep=2)
    rows = db.query(
        "SELECT note, rows_affected, status FROM task_runs"
        " WHERE job_name = 'prune' ORDER BY rowid"
    ).to_dicts()
    assert [row["note"] for row in rows] == [
        "quotes_snapshot",
        "task_runs",
        "scheduler_heartbeats",
    ]
    assert [row["rows_affected"] for row in rows] == [2, 2, 3]
    assert all(row["status"] == "ok" for row in rows)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"quotes_sessions": 0},
        {"task_runs_days": 0},
        {"heartbeats_keep": 0},
    ],
)
def test_zero_retention_is_refused(db, kwargs):
    with pytest.raises(ValidationError):
        make(db).run(**kwargs)
    assert db.row_count("task_runs") == 0


def test_a_failing_step_rolls_back_the_earlier_deletes(db, monkeypatch):
    """三个删除是一个写单元：心跳那一步失败时，报价与台账已经删掉的行必须回来。

    否则「库磁盘满了」的后果不是一次失败的清理，而是「快照少了两天、台账多了三行
    写着已清理」——下一次清理看到的是一份没删干净的账。
    """
    seed_quotes(db)
    seed_task_runs(db)
    seed_heartbeats(db)
    service = make(db)

    def boom(_keep):
        raise RuntimeError("磁盘满")

    monkeypatch.setattr(service.heartbeats, "delete_beyond", boom)
    with pytest.raises(RuntimeError, match="磁盘满"):
        service.run(quotes_sessions=5, task_runs_days=90, heartbeats_keep=2)
    assert db.row_count("quotes_snapshot") == 10
    assert db.row_count("task_runs") == 3
    assert db.row_count("scheduler_heartbeats") == 5
