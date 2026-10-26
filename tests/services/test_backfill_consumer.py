"""每拍至多消费 limit 条：单票失败记账不炸拍（D144）。"""

from datetime import UTC, datetime

import pytest

from sams.core.errors import DatasourceError
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
    done = BackfillConsumer(sync, queued, settings).consume(MOMENT)
    assert done == 3  # 一拍只吃 limit=3，第四条留到下拍
    assert queued.open_for("A", "600519") is None
    assert queued.open_for("A", "601299") is not None
    # 窗口 = 回补 730 天到当地今天
    _, _, start, end = sync.calls[0]
    assert (end - start).days == 730


def test_failure_bookkeeping(settings, queued):
    sync = StubSync([DatasourceError("t", "源挂了"), _summary(empty=True), _summary()])
    BackfillConsumer(sync, queued, settings).consume(MOMENT)
    row = queued.open_for("A", "600519")
    # `str(DatasourceError)` 带 `[source] ` 前缀（errors.py 现状）：记账原样存这一句
    assert (row.tries, row.last_error) == (1, "[t] 源挂了")
    assert queued.open_for("A", "000001").last_error == "区间内所有源均无数据"


def test_three_fails_defer_and_stop_pending(settings, db):
    repo = SymbolBackfillRepo(db)
    repo.enqueue("A", "600519")
    sync = StubSync([DatasourceError("t", "挂")] * 3)
    consumer = BackfillConsumer(sync, repo, settings, limit=1)
    for _ in range(3):
        consumer.consume(MOMENT)
    assert repo.open_for("A", "600519").status == "deferred"
    assert consumer.consume(MOMENT) == 0  # deferred 不是 pending：不再占拍，让位盘后任务
