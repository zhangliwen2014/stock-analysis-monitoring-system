"""登记兴趣：主档没有就建行（代码表有名字就用名字），缺什么就排队（D144/D283）。"""

from datetime import UTC, date, datetime, timedelta

import polars as pl
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
            },
            # 600519 也得在册：D314 之后「不在名单里」的票根本不排队，
            # 那几条 stale 用例要测的就是「在册、但落后市场最大值」这一档
            {
                "code": "600519",
                "name": "贵州茅台",
                "py_initials": "gzmt",
                "py_full": "guizhoumaotai",
                "currency": "CNY",
            },
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


def test_a_code_the_table_does_not_know_queues_nothing(db, parts):
    """代码表不认识 = 向源问它注定失败：主档行照建，两条补采都不排（D314）。

    `A/000996` 那行 `deferred / tries=3` 就是这么来的：点开过一次就排一条每晚白试三次的活，
    而 #381 刚把补数的喘息口开进按只循环——明知失败的活排得越勤，越吃掉真缺口的份额。
    """
    symbols, backfills, interest = parts
    report = interest.ensure("A", "000996", actor="alice")
    assert report.created_master is True
    assert report.queued is False
    assert report.actions_queued is False
    assert backfills.open_for("A", "000996") is None
    assert backfills.open_for("A", "000996", kind="actions") is None


def test_an_empty_code_table_still_queues(db, parts):
    """整表没同步过时补采照排（D314 的「空表没有否决权」）：新装库第一晚加的票不能被判死刑。"""
    _, backfills, interest = parts
    report = interest.ensure("US", "AAPL", actor="alice")
    assert report.created_master is True
    assert report.queued is True
    assert backfills.open_for("US", "AAPL") is not None


def _one_bar(market: str, code: str, day: date) -> pl.DataFrame:
    """一根 K 线：`ensure()` 问的是「这只票最新那根比市场最新那根老几天」，一根就够回答。"""
    return pl.DataFrame(
        {
            "market": [market],
            "code": [code],
            "date": [day],
            "open": [1.0],
            "high": [1.5],
            "low": [0.5],
            "close": [1.2],
            "volume": [100.0],
            "amount": [120.0],
            "source": ["akshare"],
        }
    )


def test_stale_bars_queue_a_refetch(db, parts):
    """看过的票冻在 10-29，同市场别的票已经到 11-04 ⇒ 排一条日线补采（#380 F1）。

    这一条是那六只港股的完整形状：第一次打开补了 493 行，此后每次打开 `count` 都不是 0，
    旧判据再也不问第二句，于是页面停在六个工作日之前而没有任何东西去补它。
    """
    repo = BarRepo(db)
    repo.upsert(_one_bar("A", "600519", date(2026, 10, 29)))
    repo.upsert(_one_bar("A", "000002", date(2026, 11, 4)))
    _, backfills, interest = parts
    report = interest.ensure("A", "600519", actor="alice")
    assert report.queued is True
    assert backfills.open_for("A", "600519") is not None


def test_market_latest_symbol_queues_nothing(db, parts):
    """跟上市场最大值的那只不排：否则每次渲染页面都排一条，队列被念成积压。"""
    repo = BarRepo(db)
    repo.upsert(_one_bar("A", "600519", date(2026, 11, 4)))
    repo.upsert(_one_bar("A", "000002", date(2026, 10, 29)))
    _, backfills, interest = parts
    report = interest.ensure("A", "600519", actor="alice")
    assert report.queued is False
    assert backfills.open_for("A", "600519") is None


def test_other_market_does_not_make_it_look_stale(db, parts):
    """对照只在市场内：A 股冻在 10-29 不因为港股已经到 11-04 而排队。"""
    repo = BarRepo(db)
    repo.upsert(_one_bar("A", "600519", date(2026, 10, 29)))
    repo.upsert(_one_bar("HK", "00700", date(2026, 11, 4)))
    _, backfills, interest = parts
    assert interest.ensure("A", "600519", actor="alice").queued is False


def test_stale_refetch_queues_once(db, parts):
    """落后那条只排一次：`open_key` 的去重在这条判据上同样成立。"""
    repo = BarRepo(db)
    repo.upsert(_one_bar("A", "600519", date(2026, 10, 29)))
    repo.upsert(_one_bar("A", "000002", date(2026, 11, 4)))
    _, backfills, interest = parts
    first = interest.ensure("A", "600519", actor="alice")
    second = interest.ensure("A", "600519", actor="bob")
    assert (first.queued, second.queued) == (True, False)
