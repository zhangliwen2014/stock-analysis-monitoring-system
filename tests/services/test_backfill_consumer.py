"""每拍至多消费 limit 条：单票失败记账不炸拍（D144）。"""

from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from sams.core.errors import DatasourceError
from sams.core.timeutil import HISTORY_START
from sams.services.scheduler.backfill import BackfillConsumer
from sams.services.sync import SyncSummary
from sams.storage.repos.symbol_backfills import SymbolBackfillRepo

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
