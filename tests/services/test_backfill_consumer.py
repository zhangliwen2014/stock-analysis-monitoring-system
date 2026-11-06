"""每拍至多消费 limit 条：单票失败记账不炸拍（D144）。"""

from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from support import FrozenClock

from sams.core.errors import DatasourceError
from sams.core.timeutil import HISTORY_START
from sams.services.scheduler.backfill import BackfillConsumer, BackfillPump
from sams.services.sync import SyncSummary
from sams.storage.repos.symbol_backfills import (
    FINANCIALS,
    FLOW,
    VALUATION,
    SymbolBackfillRepo,
)

MOMENT = datetime(2026, 10, 26, 3, 0, tzinfo=UTC)
class StubSync:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = []

    def sync_daily(self, market, code, start, end):
        self.calls.append((market, code, start, end))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _summary(**kw):
    base = {"market": "A", "code": "600519", "written": 3}
    base.update(kw)
    return SyncSummary(**base)


@pytest.fixture
def queued(db):
    repo = SymbolBackfillRepo(db)
    for code in ("600519", "000001", "600000", "601299"):
        repo.enqueue("A", code)
    return repo


def test_success_marks_done(settings, queued):
    sync = StubSync([_summary(), _summary(), _summary(), _summary()])
    done = BackfillConsumer(sync, StubActions(), queued, settings).consume(MOMENT)
    assert done == 3  # 一拍只吃 limit=3，第四条留到下拍
    assert queued.open_for("A", "600519") is None
    assert queued.open_for("A", "601299") is not None
    # 窗口 = 回补 730 天到当地今天
    _, _, start, end = sync.calls[0]
    assert (end - start).days == 730


def test_failure_bookkeeping(settings, queued):
    sync = StubSync([DatasourceError("t", "源挂了"), _summary(empty=True), _summary()])
    BackfillConsumer(sync, StubActions(), queued, settings).consume(MOMENT)
    row = queued.open_for("A", "600519")
    # `str(DatasourceError)` 带 `[source] ` 前缀（errors.py 现状）：记账原样存这一句
    assert (row.tries, row.last_error) == (1, "[t] 源挂了")
    assert queued.open_for("A", "000001").last_error == "区间内所有源均无数据"


def test_three_fails_defer_and_stop_pending(settings, db):
    repo = SymbolBackfillRepo(db)
    repo.enqueue("A", "600519")
    sync = StubSync([DatasourceError("t", "挂")] * 3)
    consumer = BackfillConsumer(sync, StubActions(), repo, settings, limit=1)
    for _ in range(3):
        consumer.consume(MOMENT)
    assert repo.open_for("A", "600519").status == "deferred"
    assert consumer.consume(MOMENT) == 0  # deferred 不是 pending：不再占拍，让位盘后任务


class StubActions:
    def __init__(self):
        self.calls = []

    def sync_actions(self, market, code, start, end):
        self.calls.append((market, code, start, end))
        return SimpleNamespace(source="akshare", written=0)


def test_an_actions_row_fetches_corp_actions_not_bars(settings, db):
    """`kind='actions'` 那一行补的是公司行为覆盖（spec 2026-11-03 §4）。

    分派错了这一行会白拉一遍日线：日线本来就齐（缺的只是覆盖），于是队列表面上在动，
    覆盖却永远补不上，界面上那句「后台已排队」就成了死循环的谎言。
    """
    repo = SymbolBackfillRepo(db)
    repo.enqueue("A", "600519", kind="actions")
    sync, actions = StubSync([]), StubActions()
    assert BackfillConsumer(sync, actions, repo, settings).consume(MOMENT) == 1
    assert sync.calls == []
    # 窗口是全历史到今天：覆盖确认要写到「今天为止」，按 730 天拉会留下更早的空白
    assert actions.calls == [("A", "600519", HISTORY_START, date(2026, 10, 26))]
    assert repo.open_for("A", "600519", kind="actions") is None


def test_a_bars_row_still_fetches_bars(settings, db):
    """加了一种活不能把原来那种弄丢：不传 kind 的旧行还是走 `sync_daily`。"""
    repo = SymbolBackfillRepo(db)
    repo.enqueue("A", "600519")
    sync, actions = StubSync([_summary()]), StubActions()
    assert BackfillConsumer(sync, actions, repo, settings).consume(MOMENT) == 1
    assert [call[:2] for call in sync.calls] == [("A", "600519")]
    assert actions.calls == []


def test_actions_failure_books_on_the_row(settings, db):
    """公司行为取不到要记在行上并留在队列：覆盖没确认，这条活没干完。"""
    repo = SymbolBackfillRepo(db)
    repo.enqueue("A", "600519", kind="actions")

    def boom(market, code, start, end):
        raise DatasourceError("akshare", "链上没有能采公司行为的源")

    actions, sync = StubActions(), StubSync([])
    actions.sync_actions = boom
    BackfillConsumer(sync, actions, repo, settings).consume(MOMENT)
    row = repo.open_for("A", "600519", kind="actions")
    assert (row.status, row.tries) == ("pending", 1)
    assert row.last_error == "[akshare] 链上没有能采公司行为的源"


class StubFundamentals:
    """三种基本面活的最小替身：记下每次同步的窗口，按需失败或答 0 行。"""

    def __init__(self, *, latest: date | None = None, fail: Exception | None = None):
        self.calls: list[tuple[str, str, str, date, date]] = []
        self.latest = latest
        self.fail = fail
        self.empty = False
        self.valuation = SimpleNamespace(latest_date=self._latest)
        self.flow = SimpleNamespace(latest_date=self._latest)

    def _latest(self, market, code):
        return self.latest

    def _answer(self, kind, market, code, start, end):
        if self.fail is not None:
            raise self.fail
        self.calls.append((kind, market, code, start, end))
        return SimpleNamespace(written=0 if self.empty else 2, empty=self.empty)

    def sync_financials(self, market, code, start, end):
        return self._answer("financials", market, code, start, end)

    def sync_valuation(self, market, code, start, end):
        return self._answer("valuation", market, code, start, end)

    def sync_flow(self, market, code, start, end):
        return self._answer("flow", market, code, start, end)


def test_a_financials_row_walks_the_whole_history(settings, db):
    """D330 顺延下来的财务行：报告期会重述，增量窗口会漏掉旧期的修正（与 job 同口径）。"""
    repo = SymbolBackfillRepo(db)
    repo.enqueue("A", "600519", kind=FINANCIALS)
    fundamentals = StubFundamentals()
    consumer = BackfillConsumer(
        StubSync([]), StubActions(), repo, settings, fundamentals=fundamentals
    )

    assert consumer.consume(MOMENT) == 1
    assert fundamentals.calls == [
        ("financials", "A", "600519", HISTORY_START, date(2026, 10, 26))
    ]
    assert repo.open_for("A", "600519", kind=FINANCIALS) is None


def test_a_valuation_row_starts_after_the_latest_day_on_file(settings, db):
    """估值/资金流不是全历史活：从库内最新一天续上，已追平就不问。"""
    repo = SymbolBackfillRepo(db)
    repo.enqueue("A", "600519", kind=VALUATION)
    fundamentals = StubFundamentals(latest=date(2026, 10, 20))
    consumer = BackfillConsumer(
        StubSync([]), StubActions(), repo, settings, fundamentals=fundamentals
    )

    consumer.consume(MOMENT)
    assert fundamentals.calls == [
        ("valuation", "A", "600519", date(2026, 10, 21), date(2026, 10, 26))
    ]
    assert repo.open_for("A", "600519", kind=VALUATION) is None


def test_a_caught_up_flow_row_closes_without_a_request(settings, db):
    """已经追平的行没有可问的窗口：不该空打一次源，也不该把它留在队列里。"""
    repo = SymbolBackfillRepo(db)
    repo.enqueue("A", "600519", kind=FLOW)
    fundamentals = StubFundamentals(latest=date(2026, 10, 26))
    consumer = BackfillConsumer(
        StubSync([]), StubActions(), repo, settings, fundamentals=fundamentals
    )

    consumer.consume(MOMENT)
    assert fundamentals.calls == []
    assert repo.open_for("A", "600519", kind=FLOW) is None


def test_a_flow_failure_is_booked_and_stays_pending(settings, db):
    repo = SymbolBackfillRepo(db)
    repo.enqueue("A", "600519", kind=FLOW)
    fundamentals = StubFundamentals(
        latest=date(2026, 10, 20), fail=DatasourceError("akshare", "源挂了")
    )
    consumer = BackfillConsumer(
        StubSync([]), StubActions(), repo, settings, fundamentals=fundamentals
    )

    consumer.consume(MOMENT)
    row = repo.open_for("A", "600519", kind=FLOW)
    assert (row.status, row.tries, row.last_error) == ("pending", 1, "[akshare] 源挂了")


def test_zero_rows_is_a_legal_answer_for_the_new_kinds(settings, db):
    """0 行不是失败（D36）：没有披露/没有数据是答复，不是「没补上」。"""
    repo = SymbolBackfillRepo(db)
    repo.enqueue("A", "600519", kind=FINANCIALS)
    fundamentals = StubFundamentals()
    fundamentals.empty = True
    consumer = BackfillConsumer(
        StubSync([]), StubActions(), repo, settings, fundamentals=fundamentals
    )

    consumer.consume(MOMENT)
    assert repo.open_for("A", "600519", kind=FINANCIALS) is None


def test_without_the_service_a_new_kind_row_is_not_silently_closed(settings, db):
    """消费者没接基本面服务时，把顺延行当 done 等于悄悄抹掉一笔账。"""
    repo = SymbolBackfillRepo(db)
    repo.enqueue("A", "600519", kind=VALUATION)
    consumer = BackfillConsumer(StubSync([]), StubActions(), repo, settings)

    consumer.consume(MOMENT)
    row = repo.open_for("A", "600519", kind=VALUATION)
    assert (row.status, row.tries) == ("pending", 1)
    assert "没有基本面服务" in row.last_error


class SteppingMonotonic:
    """每问一次走一步的单调钟：那一步就是「两次喘息之间过了多久」。"""

    def __init__(self, step_s: float) -> None:
        self.step_s = step_s
        self._now = 0.0

    def __call__(self) -> float:
        value = self._now
        self._now += self.step_s
        return value


def test_the_pump_gives_the_queue_a_breath_only_every_min_gap(db, settings):
    """泵每 20 秒真动手一次：一条 505 只的循环里它会被叫五百次，绝大多数要看一眼就返回。

    闸落在「上次真的动手」那一刻上，不是「上次被调用」——否则一直有队列可拿的那一段
    会把闸不断往后推，反而补得更慢。
    """
    repo = SymbolBackfillRepo(db)
    for code in ("600519", "000001", "600000", "601299"):
        repo.enqueue("A", code)
    sync = StubSync([_summary(), _summary(), _summary(), _summary()])
    consumer = BackfillConsumer(sync, StubActions(), repo, settings, limit=1)
    pump = BackfillPump(
        consumer,
        clock=FrozenClock(MOMENT),
        monotonic=SteppingMonotonic(10.0),
    )

    pump()  # 0 秒：动手，消化一条
    pump()  # 10 秒：离上次动手不到 20 秒，看一眼就返回
    pump()  # 20 秒：又到期，再消化一条

    assert [call[1] for call in sync.calls] == ["600519", "000001"]
