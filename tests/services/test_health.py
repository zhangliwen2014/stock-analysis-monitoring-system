from __future__ import annotations

from datetime import date

import polars as pl
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
    # 一年「已覆盖」= 首尾都铺到（见 CalendarRepo.year_covered），只写一天不算
    days = [
        CalendarDay(
            "A", D, True, False, "Asia/Shanghai", (SessionWindow("am", "09:30", "11:30"),)
        ),
        CalendarDay("A", date(2024, 12, 30), True, False, "Asia/Shanghai", ()),
        # 上半年有、下半年没有：来源给半截时这一年必须报未覆盖，否则 Jul~Dec 静默停摆
        CalendarDay("US", date(2024, 4, 1), True, False, "America/New_York", ()),
    ]
    CalendarRepo(db).save(days, source="fake")
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


def test_never_attempted_source_is_unverified_not_degraded(db, settings):
    """未调用的源既不算故障也不能被忽略：它要出现在报告里，但不把退出码变成失败。"""
    registry = AdapterRegistry(
        adapters={"yfinance": StubAdapter("yfinance", ["US"], sample_bars("US", "AAPL"))},
        market_sources={"US": ("yfinance",)},
        sleep=lambda seconds: None,
    )
    report = HealthService(db, registry, CalendarRepo(db), settings).report()
    assert [s.name for s in report.unverified_sources] == ["yfinance"]
    assert report.degraded_sources == []


def test_calendar_window_is_taken_from_the_injected_clock(db, settings):
    """健康报告的「今年」来自注入的时钟：用墙钟的话，同一次重放换个日子跑结论就不同。"""
    from datetime import UTC, datetime

    from support import FrozenClock

    asked: list[int] = []

    class RecordingStore:
        def year_covered(self, market: str, year: int, *, through=None) -> bool:
            asked.append(year)
            return False

    service = HealthService(
        db,
        AdapterRegistry(adapters={}, market_sources={}, sleep=lambda s: None),
        RecordingStore(),
        settings,
        clock=FrozenClock(datetime(2030, 5, 1, tzinfo=UTC)),
        year_horizon=1,
    )
    assert service.report().calendar_years["A"] == []
    assert asked == [2029, 2030, 2031] * len(settings.markets)


# ---------------------------------------------------------------- 日线事实


def test_report_carries_freshness_and_coverage_per_market(db, settings):
    """行数说明不了「能不能用」：最后一天是哪天、成交额有没有值才是。

    `bars_daily = 30000` 可能是 300 只票十年，也可能是三天前停更的三只。
    """
    BarRepo(db).upsert(sample_bars("A", "600519"))
    no_amount = sample_bars("HK", "00700").with_columns(
        pl.lit(None, dtype=pl.Float64).alias("amount")
    )
    BarRepo(db).upsert(no_amount)
    report = service(db, settings, StubAdapter("akshare", ["A"], sample_bars())).report()
    data = report.market_data
    assert data["A"].rows == 2 and data["A"].codes == 1
    assert data["A"].latest == date(2024, 1, 3)
    assert data["A"].with_amount == 2 and data["A"].amount_missing == 0
    # 只有 tencent/sina 在服务时成交额整段为空：不是错误，但依赖它的规则永不触发
    assert data["HK"].amount_missing == 2


def test_negative_volume_is_counted_as_a_quality_fact(db, settings):
    """负量原样入库是决定，但它必须在这里露头，否则量比类指标会算出 62 倍。"""
    frame = sample_bars().with_columns(
        pl.when(pl.col("date") == D).then(-5.0).otherwise(pl.col("volume")).alias("volume")
    )
    BarRepo(db).upsert(frame)
    report = service(db, settings, StubAdapter("akshare", ["A"], sample_bars())).report()
    data = report.market_data
    assert data["A"].negative_volume == 1


def test_a_market_without_bars_has_no_data_row(db, settings):
    """还没采集过 ≠ 数据坏了：报告里根本没有这一项，由 CLI 说「尚未采集」。"""
    report = service(db, settings, StubAdapter("akshare", ["A"], sample_bars())).report()
    assert report.market_data == {}
    assert report.tables["corp_actions"] == 0, "裸价表空着是跨除权日涨跌不可用的前提事实"


def test_the_bar_lag_survives_a_later_write(db, settings):
    BarRepo(db).upsert(sample_bars())
    health = service(db, settings, StubAdapter("akshare", ["A"], sample_bars()))
    assert health.report().market_data["A"].latest == date(2024, 1, 3)
    BarRepo(db).upsert(sample_bars(start=date(2024, 4, 1)))
    assert health.report().market_data["A"].latest == date(2024, 4, 2)
