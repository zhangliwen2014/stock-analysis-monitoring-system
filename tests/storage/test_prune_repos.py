"""保留策略的仓库层：删除边界只看三个数——日期、天数、条数。

这些用例直接写表而不经过服务：要钉的是「第 N 个交易日」与「第 N 条心跳」这两个
边界本身。服务层的用例再钉「dry-run 与实删用的是同一份计数」。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest
from support import FrozenClock

from sams.core.errors import ValidationError
from sams.storage.repos.quotes import QuoteRepo
from sams.storage.repos.task_runs import HeartbeatRepo, TaskRunRepo


def add_quote(db, market, code, day, *, price=10.0):
    db.execute(
        "INSERT INTO quotes_snapshot (market, code, ts, price, source)"
        " VALUES (?, ?, ?, ?, 'test')",
        [market, code, datetime.combine(day, time(2, 0), tzinfo=UTC), price],
    )


def test_session_cutoff_is_the_nth_newest_day_per_market(db):
    """边界按**市场各数各的**：A 股长假那一周港股照常开市，按全库日期数会把
    A 股节前最后一个交易日的快照提前删掉——那正是复盘最需要的一屏。
    """
    for offset in range(7):
        add_quote(db, "HK", "00700", date(2026, 10, 1) + timedelta(days=offset))
    for offset in range(3):
        add_quote(db, "A", "600519", date(2026, 10, 1) + timedelta(days=offset))
    # HK 的第 5 新是 10-03；A 只有 3 天，不足 5 天就整段保留（不出现在字典里）
    assert QuoteRepo(db).session_cutoffs(5) == {"HK": date(2026, 10, 3)}


def test_session_cutoff_rejects_zero(db):
    with pytest.raises(ValidationError, match="keep_sessions"):
        QuoteRepo(db).session_cutoffs(0)


def test_count_then_delete_removes_only_rows_before_each_cutoff(db):
    for offset in range(7):
        add_quote(db, "HK", "00700", date(2026, 10, 1) + timedelta(days=offset))
    for offset in range(3):
        add_quote(db, "A", "600519", date(2026, 10, 1) + timedelta(days=offset))
    repo = QuoteRepo(db)
    cutoffs = repo.session_cutoffs(5)
    assert repo.count_before(cutoffs) == 2
    repo.delete_before(cutoffs)
    remaining = db.query(
        "SELECT market, count(*) AS n FROM quotes_snapshot GROUP BY market ORDER BY market"
    ).to_dicts()
    assert remaining == [{"market": "A", "n": 3}, {"market": "HK", "n": 5}]


def test_an_empty_cutoff_map_deletes_nothing(db):
    """一个市场都没有 5 天历史时，字典是空的：空字典不是「全删」的信号。"""
    add_quote(db, "A", "600519", date(2026, 10, 1))
    repo = QuoteRepo(db)
    assert repo.count_before({}) == 0
    repo.delete_before({})
    assert db.row_count("quotes_snapshot") == 1


def test_task_runs_older_than_the_cutoff_are_counted_and_deleted(db):
    repo = TaskRunRepo(db)
    for run_id, started in (
        ("old", datetime(2025, 1, 1, tzinfo=UTC)),
        ("edge", datetime(2026, 1, 1, tzinfo=UTC)),
        ("new", datetime(2026, 6, 1, tzinfo=UTC)),
    ):
        db.execute(
            "INSERT INTO task_runs (id, job_name, started_at, status)"
            " VALUES (?, 'bars', ?, 'ok')",
            [run_id, started],
        )
    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    assert repo.count_before(cutoff) == 1, "等于边界的那一行要留下：判据是 < 不是 <="
    repo.delete_before(cutoff)
    assert [row.id for row in repo.recent("bars", None, limit=10)] == ["new", "edge"]


def test_heartbeats_beyond_the_keep_count_are_the_oldest(db):
    clock = FrozenClock(datetime(2026, 10, 1, tzinfo=UTC))
    repo = HeartbeatRepo(db, clock=clock)
    ids = []
    for _ in range(4):
        ids.append(repo.start(note="tick=60s"))
        clock.advance(minutes=1)
    assert repo.count_beyond(2) == 2
    repo.delete_beyond(2)
    left = db.query("SELECT id FROM scheduler_heartbeats ORDER BY beat_at").to_dicts()
    assert [row["id"] for row in left] == ids[-2:]


def test_heartbeats_keep_rejects_zero(db):
    with pytest.raises(ValidationError, match="keep"):
        HeartbeatRepo(db).count_beyond(0)
    with pytest.raises(ValidationError, match="keep"):
        HeartbeatRepo(db).delete_beyond(0)
