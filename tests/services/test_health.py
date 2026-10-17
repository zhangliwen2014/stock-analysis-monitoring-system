from __future__ import annotations

from datetime import date

import pytest
from support import StubAdapter, sample_bars

from sams.core.errors import DatasourceError, RateLimited
from sams.core.timeutil import CalendarDay, SessionWindow
from sams.datasource.registry import AdapterRegistry
from sams.services.health import KEY_TABLES, HealthService
from sams.storage.db import Database
from sams.storage.migrate import discover
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.calendar import CalendarRepo
from sams.storage.repos.users import UserRepo
from sams.storage.repos.watchlists import WatchlistRepo

D = date(2024, 1, 2)
PASSWORD = "correct horse battery"


def service(db, settings, *adapters) -> HealthService:
    registry = AdapterRegistry(
        adapters={a.name: a for a in adapters},
        market_sources={"A": tuple(a.name for a in adapters)},
        sleep=lambda seconds: None,
    )
    return HealthService(db, registry, CalendarRepo(db), settings)


def test_report_covers_every_key_table(db, settings):
    report = service(db, settings, StubAdapter("akshare", ["A"], sample_bars())).report()
    assert set(report.tables) == set(KEY_TABLES)
    assert all(rows == 0 for rows in report.tables.values())
    assert report.pending_migrations == []


def test_counts_reflect_written_rows(db, settings):
    BarRepo(db).upsert(sample_bars())
    UserRepo(db).create("zhangsan", PASSWORD)
    WatchlistRepo(db).add("u1", "A", "600519")
    report = service(db, settings, StubAdapter("akshare", ["A"], sample_bars())).report()
    assert report.tables["bars_daily"] == 2
    assert report.tables["users"] == 1
    assert report.tables["watchlists"] == 1


def test_calendar_coverage_lists_covered_years_only(db, settings):
    day = CalendarDay(
        "A", D, True, False, "Asia/Shanghai", (SessionWindow("am", "09:30", "11:30"),)
    )
    CalendarRepo(db).save([day], source="fake")
    report = service(db, settings, StubAdapter("akshare", ["A"], sample_bars())).report()
    assert report.calendar_years["A"] == [D.year]
    assert report.calendar_years["US"] == []


def test_router_failures_show_up_in_the_report(db, settings):
    good = StubAdapter("akshare", ["A"], sample_bars())
    bad = StubAdapter("eastmoney", ["A"], RateLimited("eastmoney", "429"))
    svc = service(db, settings, good, bad)

    svc.registry.fetch_daily("A", "600519", D, D)
    assert svc.report().degraded_sources == [], "成功的源不该被记成故障"

    svc.registry.market_sources["A"] = ("eastmoney",)
    with pytest.raises(DatasourceError):
        svc.registry.fetch_daily("A", "600519", D, D)

    degraded = svc.report().degraded_sources
    assert [s.name for s in degraded] == ["eastmoney"]
    assert degraded[0].consecutive_failures == 1
    assert "429" in degraded[0].last_error


def test_unmigrated_database_lists_pending(tmp_path, settings):
    fresh = Database.open(tmp_path / "fresh.duckdb")
    try:
        report = service(fresh, settings, StubAdapter("akshare", ["A"], sample_bars())).report()
        assert report.pending_migrations == [p.name for p in discover()]
        assert report.tables == {}
        assert report.calendar_years == {}, "未建库时日历覆盖应报空而不是崩"
    finally:
        fresh.close()
