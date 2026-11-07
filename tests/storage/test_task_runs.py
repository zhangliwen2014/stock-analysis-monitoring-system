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


def test_latest_per_period_reports_only_the_last_row_of_a_period(db):
    """一个周期的结论就是它的最后一行：更早的重试行不能替这个周期说话。

    两行之间推进了时钟，所以这一枚钉的是「按周期取最新一行」本身，
    不依赖同一时刻多行之间的 `rowid` 决胜。
    """
    clock = Clock(datetime(2024, 10, 25, 8, 30, tzinfo=UTC))
    repo = TaskRunRepo(db, clock=clock)
    failed = repo.start("bars", market="A", period_key="2024-10-25")
    repo.finish(failed, status=FAILED, error="boom")
    clock.advance(minutes=1)
    ok = repo.start("bars", market="A", period_key="2024-10-25")
    repo.finish(ok, status=OK)
    latest = repo.latest_per_period("bars", "A", limit=5)
    assert [row.id for row in latest] == [ok]
    assert latest[0].status == OK


def test_two_rows_at_the_same_instant_report_the_last_written(db):
    """同一瞬间写下的两行，报后写的那一行：决胜靠 `rowid`，不靠运气。

    去掉分组排序里的 `rowid DESC`，DuckDB 会把**先**写的那一行当成这个周期的结论：
    一个先失败、后跑成的周期就被数成一个失败周期，连续三个周期的告警因此能凑满，
    而这一晚其实一次都没打挂。「最近的一行」如果由存储引擎的心情决定，就不是最近的一行。
    """
    clock = Clock(datetime(2024, 10, 25, 8, 30, tzinfo=UTC))
    repo = TaskRunRepo(db, clock=clock)
    first = repo.start("bars", market="A", period_key="2024-10-25")
    repo.finish(first, status=FAILED, error="第一次")
    second = repo.start("bars", market="A", period_key="2024-10-25")
    repo.finish(second, status=OK)
    latest = repo.latest_per_period("bars", "A", limit=5)
    assert [row.id for row in latest] == [second]


def test_latest_per_period_limit_counts_periods_not_rows(db):
    """`limit` 数的是**周期**：一个周期无论写了多少行，都只占一格。"""
    clock = Clock(datetime(2024, 10, 24, 8, 30, tzinfo=UTC))
    repo = TaskRunRepo(db, clock=clock)
    last_ids = []
    for period in ("2024-10-24", "2024-10-25", "2024-10-26"):
        for _ in range(3):
            run_id = repo.start("bars", market="A", period_key=period)
            repo.finish(run_id, status=FAILED, error="boom")
            clock.advance(minutes=1)
        last_ids.append(run_id)
    latest = repo.latest_per_period("bars", "A", limit=2)
    assert [row.id for row in latest] == list(reversed(last_ids))[:2]
    assert [row.period_key for row in latest] == ["2024-10-26", "2024-10-25"]


def test_latest_per_period_does_not_leak_across_markets(db):
    """两个方向都要钉住：全局账本看不见 A 股的行，A 股的账本也看不见全局的行。

    漏了 `market` 的话，一个账本的失败周期会去数进另一个账本的连续失败里。
    """
    clock = Clock(datetime(2024, 10, 25, 8, 30, tzinfo=UTC))
    repo = TaskRunRepo(db, clock=clock)
    for period in ("2024-10-25", "2024-10-26"):
        global_run = repo.start("fx", period_key=period)
        repo.finish(global_run, status=OK)
        market_run = repo.start("fx", market="A", period_key=period)
        repo.finish(market_run, status=OK)
        clock.advance(minutes=1)
    global_rows = repo.latest_per_period("fx", None, limit=5)
    assert [row.market for row in global_rows] == [None, None]
    market_rows = repo.latest_per_period("fx", "A", limit=5)
    assert [row.market for row in market_rows] == ["A", "A"]


def test_history_filters_pages_and_counts_the_unfiltered_total(db):
    """管理页的台账：筛选条件与总数必须来自同一组 where，否则「共 N 条」会骗人。"""
    clock = Clock(datetime(2024, 10, 25, 8, 30, tzinfo=UTC))
    repo = TaskRunRepo(db, clock=clock)
    ids = []
    for job, market, status in (
        ("bars", "A", OK),
        ("valuation", "A", FAILED),
        ("bars", "HK", FAILED),
    ):
        run_id = repo.start(job, market=market, period_key="2024-10-25")
        repo.finish(run_id, status=status, error="boom" if status == FAILED else None)
        ids.append(run_id)
        clock.advance(minutes=1)

    rows, total = repo.history()
    assert total == 3
    assert [row.id for row in rows] == list(reversed(ids))
    rows, total = repo.history(market="HK")
    assert total == 1 and rows[0].job_name == "bars"
    rows, total = repo.history(status=FAILED)
    assert total == 2 and {row.market for row in rows} == {"A", "HK"}
    rows, total = repo.history(page=2, per_page=2)
    assert total == 3 and [row.id for row in rows] == [ids[0]]


def test_failed_names_for_one_period_names_each_job_once(db):
    """摘要要说的是一句「哪些 job 没跑成」，不是每个重试一行。"""
    clock = Clock(datetime(2024, 10, 25, 8, 30, tzinfo=UTC))
    repo = TaskRunRepo(db, clock=clock)
    bars = repo.start("bars", market="A", period_key="2024-10-25")
    repo.finish(bars, status=FAILED, error="源超时")
    retry = repo.start("bars", market="A", period_key="2024-10-25")
    repo.finish(retry, status=FAILED, error="源超时（第二次）")
    valuation = repo.start("valuation", market="A", period_key="2024-10-25")
    repo.finish(valuation, status=FAILED, error="空答复")
    ok = repo.start("spot", market="A", period_key="2024-10-25")
    repo.finish(ok, status=OK)
    other_day = repo.start("rules", market="A", period_key="2024-10-24")
    repo.finish(other_day, status=FAILED, error="昨天的")
    other_market = repo.start("bars", market="HK", period_key="2024-10-25")
    repo.finish(other_market, status=FAILED, error="香港的")
    assert repo.failed_names_for("A", "2024-10-25") == ("bars", "valuation")
    assert repo.failed_names_for("A", "2024-10-25", exclude=("bars",)) == ("valuation",)


def test_close_interrupted_finishes_every_running_row_and_only_those(db):
    """进程被杀会留下一堆 `running`：新进程一启动就该把它们结掉。

    库里单写者，所以「有一行 running」只有两种解释：本进程正在跑（启动时不可能），
    或上一个进程死了没收尾。留着不结，`sams health` 就把三天前那次死掉的运行读成
    「正在进行」，而那一行的周期再也不会被算进「这一晚哪些没跑成」。
    """
    clock = Clock(datetime(2024, 10, 25, 8, 30, tzinfo=UTC))
    repo = TaskRunRepo(db, clock=clock)
    orphan_a = repo.start("financials", market="A", period_key="2024-10-25")
    orphan_hk = repo.start("flow", market="HK", period_key="2024-10-25")
    done = repo.start("bars", market="A", period_key="2024-10-25")
    repo.finish(done, status=OK, rows_affected=7)
    clock.advance(hours=3)

    assert repo.close_interrupted() == 2

    for run_id in (orphan_a, orphan_hk):
        row = repo.get(run_id)
        assert row.status == FAILED
        assert row.finished_at == datetime(2024, 10, 25, 11, 30, tzinfo=UTC)
        assert "未收尾" in (row.error or "")
    assert repo.get(done).status == OK
    # 结过一次就幂等：第二次没有孤儿可结
    assert repo.close_interrupted() == 0


def test_running_names_the_rows_that_have_not_finished_newest_first(db):
    """「现在还有没有人在做活」是健康读数要分开的另一半：一行 `running` 就是证据。

    这一句可信的前提是上面那个测试：单写者 + 新进程启动时结孤儿，所以今天读到的
    `running` 不可能是上一个死掉的进程留下的。顺序按开始时间新到旧——同时挂着几行时，
    健康页要说的是「正在跑『最近那一个』」。
    """
    clock = Clock(datetime(2024, 10, 25, 8, 30, tzinfo=UTC))
    repo = TaskRunRepo(db, clock=clock)
    first = repo.start("bars", market="A", period_key="2024-10-25")
    clock.advance(minutes=2)
    repo.start("financials", market="A", period_key="2024-10-25")
    finished = repo.start("flow", market="HK", period_key="2024-10-25")
    repo.finish(finished, status=OK, rows_affected=1)

    rows = repo.running()

    assert [row.job_name for row in rows] == ["financials", "bars"]
    assert rows[1].id == first
    assert all(row.status == RUNNING for row in rows)
