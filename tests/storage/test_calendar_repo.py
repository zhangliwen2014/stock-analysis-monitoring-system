from __future__ import annotations

from datetime import date

import pytest

from sams.core.errors import StorageError
from sams.core.timeutil import CalendarDay, SessionWindow
from sams.storage.repos.calendar import CalendarRepo

AM = SessionWindow("am", "09:30", "11:30")
PM = SessionWindow("pm", "13:00", "15:00")


@pytest.fixture
def repo(db) -> CalendarRepo:
    return CalendarRepo(db)


def day(market: str = "A", d: date = date(2026, 3, 9), **kw) -> CalendarDay:
    return CalendarDay(
        market=market,
        day=d,
        is_trading=True,
        is_half_day=False,
        tz="Asia/Shanghai",
        windows=(AM, PM),
        **kw,
    )


def test_round_trip_preserves_sessions(repo):
    original = day()
    repo.save([original], source="akshare:sina")
    loaded = repo.get("A", original.day)
    assert loaded == original


def test_save_is_idempotent(repo):
    assert repo.save([day()], source="akshare:sina") == 1
    repo.save([day()], source="akshare:sina")
    assert repo.rows_for_year("A", 2026) == 1


def test_unknown_day_returns_none(repo):
    repo.save([day()], source="akshare:sina")
    assert repo.get("A", date(2026, 10, 1)) is None, "没入库不等于休市，必须由服务层判覆盖"
    assert not repo.year_covered("A", 2027)
    assert repo.year_covered("A", 2026)


def test_non_trading_row_can_be_stored_explicitly(repo):
    off = CalendarDay("A", date(2026, 10, 1), False, False, "Asia/Shanghai", ())
    repo.save([off], source="akshare:sina")
    loaded = repo.get("A", date(2026, 10, 1))
    assert loaded is not None and not loaded.is_trading and loaded.windows == ()


def test_half_day_flag_survives(repo):
    half = CalendarDay(
        "HK",
        date(2026, 12, 24),
        True,
        True,
        "Asia/Hong_Kong",
        (SessionWindow("regular", "09:30", "12:30"),),
    )
    repo.save([half], source="exchange_calendars:XHKG")
    assert repo.get("HK", half.day) == half


def test_trading_days_counts_only_trading(repo):
    repo.save(
        [
            day(d=date(2026, 3, 9)),
            CalendarDay("A", date(2026, 3, 10), False, False, "Asia/Shanghai", ()),
        ],
        source="akshare:sina",
    )
    assert repo.trading_days("A", 2026) == 1
    assert repo.rows_for_year("A", 2026) == 2
    assert repo.source_of("A", 2026) == "akshare:sina"


def test_empty_batch_writes_nothing(repo):
    assert repo.save([], source="x") == 0
    assert repo.source_of("A", 2026) is None


def test_corrupt_json_fails_loudly(repo, db):
    repo.save([day()], source="akshare:sina")
    db.execute("UPDATE trading_calendar SET sessions_json = '{oops'")
    with pytest.raises(StorageError, match="sessions_json"):
        repo.get("A", date(2026, 3, 9))
