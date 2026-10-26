"""登记兴趣：主档没有就建行（代码表有名字就用名字），日线为零就排队（D144）。"""

import pytest

from sams.services.symbol_interest import SymbolInterest
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.symbol_backfills import SymbolBackfillRepo
from sams.storage.repos.symbol_list import SymbolListRepo
from sams.storage.repos.symbols import SymbolRepo


@pytest.fixture
def parts(db, settings):
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
