"""登记兴趣：主档没有就建行（代码表有名字就用名字），缺什么就排队（D144/D283）。"""

from datetime import UTC, date, datetime, timedelta

import pytest
from support import FrozenClock

from sams.services.symbol_interest import SymbolInterest
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.corp_actions_coverage import CorpActionsCoverageRepo
from sams.storage.repos.symbol_backfills import SymbolBackfillRepo
from sams.storage.repos.symbol_list import SymbolListRepo
from sams.storage.repos.symbols import SymbolRepo

TODAY = date(2026, 11, 3)


@pytest.fixture
def clock():
    return FrozenClock(datetime(2026, 11, 3, 15, tzinfo=UTC))


@pytest.fixture
def coverage(db):
    return CorpActionsCoverageRepo(db)


@pytest.fixture
def parts(db, settings, clock, coverage):
    symbols = SymbolRepo(db)
    listed = SymbolListRepo(db)
    listed.upsert_batch(
        "A",
        [
            {
                "code": "000001",
                "name": "平安银行",
                "py_initials": "payh",
                "py_full": "pinganyinhang",
                "currency": "CNY",
            }
        ],
    )
    backfills = SymbolBackfillRepo(db)
    interest = SymbolInterest(
        symbols=symbols,
        bars=BarRepo(db),
        symbol_list=listed,
        backfills=backfills,
        markets=settings.markets,
        coverage=coverage,
        clock=clock,
    )
    return symbols, backfills, interest


def test_creates_master_with_name(db, parts):
    symbols, backfills, interest = parts
    report = interest.ensure("A", "000001", actor="alice")
    assert (report.market, report.code) == ("A", "000001")
    assert report.created_master is True
    row = symbols.get("A", "000001")
    assert row["name"] == "平安银行" and row["currency"] == "CNY"
    assert backfills.open_for("A", "000001") is not None  # 无日线 → 排队


def test_unknown_code_still_creates_bare_master(db, parts):
    symbols, _, interest = parts
    report = interest.ensure("A", "601299", actor="alice")
    assert report.created_master is True
    assert symbols.get("A", "601299")["name"] is None  # 代码表查不到也先立行，名字后补


def test_existing_bars_no_queue(db, parts, bars_frame):
    symbols, backfills, interest = parts
    BarRepo(db).upsert(bars_frame)  # support.bars_frame 就是 A/600519 三天
    report = interest.ensure("A", "600519", actor="alice")
    assert report.created_master is True and report.queued is False
    assert backfills.open_for("A", "600519") is None


def test_ensure_twice_is_quiet(db, parts):
    _, backfills, interest = parts
    interest.ensure("A", "000001", actor="alice")
    second = interest.ensure("A", "000001", actor="alice")
    assert (second.created_master, second.queued) == (False, False)  # 开放队列不重插


def test_no_coverage_confirmation_queues_actions(db, parts, bars_frame):
    """有日线、没覆盖确认：缺的是公司行为，不是行情（D283）。

    页面那句「先跑 `sams sync actions`」之所以是错的，是因为网页用户没有命令行、
    而 `sams serve` 占着库时命令行也跑不动——缺数据只能让后台自己补。
    """
    BarRepo(db).upsert(bars_frame)  # support.bars_frame 就是 A/600519 三天
    _, backfills, interest = parts
    report = interest.ensure("A", "600519", actor="alice")
    assert report.actions_queued is True
    assert report.queued is False  # 有日线，别再排一条 bars
    assert backfills.open_for("A", "600519", kind="actions") is not None


def test_full_history_confirmed_today_queues_nothing(db, parts, coverage, bars_frame):
    BarRepo(db).upsert(bars_frame)
    coverage.record(
        "A",
        "600519",
        covered_from=date(2010, 1, 1),
        covered_to=TODAY,
        full_history=True,
        src="akshare",
    )
    _, backfills, interest = parts
    report = interest.ensure("A", "600519", actor="alice")
    assert report.actions_queued is False
    assert backfills.open_for("A", "600519", kind="actions") is None


def test_partial_source_confirmed_today_still_queues(db, parts, coverage, bars_frame):
    """非全历史的那条确认不顶数：它只承诺自己的窗口，往后没人看过。"""
    BarRepo(db).upsert(bars_frame)
    coverage.record(
        "A",
        "600519",
        covered_from=date(2010, 1, 1),
        covered_to=TODAY,
        full_history=False,
        src="eastmoney",
    )
    _, backfills, interest = parts
    assert interest.ensure("A", "600519", actor="alice").actions_queued is True
    assert backfills.open_for("A", "600519", kind="actions") is not None


def test_stale_confirmation_queues_once_a_day(db, parts, coverage, bars_frame):
    """确认只到上周 → 排队；一天里渲染多少次都只有一条开放行。"""
    BarRepo(db).upsert(bars_frame)
    coverage.record(
        "A",
        "600519",
        covered_from=date(2010, 1, 1),
        covered_to=TODAY - timedelta(days=7),
        full_history=True,
        src="akshare",
    )
    _, backfills, interest = parts
    first = interest.ensure("A", "600519", actor="alice")
    second = interest.ensure("A", "600519", actor="bob")
    assert (first.actions_queued, second.actions_queued) == (True, False)
    assert backfills.open_for("A", "600519", kind="actions") is not None
