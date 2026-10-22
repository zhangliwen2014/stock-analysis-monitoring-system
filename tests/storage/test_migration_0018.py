"""0018：调度器的账。

这一枚只验形状：旧式 INSERT（不写新列）落下的行读出来 `period_key`/`note` 是 NULL，
心跳表能存能读且 `beat()` 真的推进时间。行为契约在 `test_task_runs.py`。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from support import FrozenClock

from sams.storage.repos.task_runs import HeartbeatRepo


def test_task_runs_gains_period_key_and_note_nullable(db):
    columns = db.columns("task_runs")
    assert "period_key" in columns
    assert "note" in columns
    # 老行不会被填上一个假的周期键：NULL 是「不知道」，不是「今天」
    db.execute(
        "INSERT INTO task_runs (id, job_name, market, started_at, status)"
        " VALUES ('old', 'bars', 'A', ?, 'ok')",
        [datetime(2024, 1, 2, tzinfo=UTC)],
    )
    row = db.query("SELECT period_key, note FROM task_runs WHERE id = 'old'").to_dicts()[0]
    assert row == {"period_key": None, "note": None}


def test_heartbeats_table_round_trips(db):
    clock = FrozenClock(datetime(2024, 10, 25, 8, 30, tzinfo=UTC))
    repo = HeartbeatRepo(db, clock=clock)
    beat_id = repo.start(note="tick=60s")
    started = repo.latest().started_at
    clock.advance(seconds=60)
    repo.beat(beat_id)
    latest = repo.latest()
    assert latest is not None
    assert latest.id == beat_id
    assert latest.note == "tick=60s"
    assert latest.started_at == started
    assert latest.beat_at == started + timedelta(seconds=60)
