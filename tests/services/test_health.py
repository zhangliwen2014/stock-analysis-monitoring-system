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
from sams.storage.repos.financials import FinancialRepo, FundingFlowRepo, ValuationRepo
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


# ---------------------------------------------------------------- 基本面覆盖


FIN_SCHEMA = {
    "market": pl.Utf8,
    "code": pl.Utf8,
    "report_date": pl.Date,
    "published_date": pl.Date,
    "roe": pl.Float64,
    "revenue_yoy": pl.Float64,
    "profit_yoy": pl.Float64,
    "dividend_yield": pl.Float64,
    "market_cap": pl.Float64,
    "source": pl.Utf8,
}
VAL_SCHEMA = {
    "market": pl.Utf8,
    "code": pl.Utf8,
    "date": pl.Date,
    "pe_ttm": pl.Float64,
    "pb": pl.Float64,
    "dividend_yield": pl.Float64,
    "market_cap": pl.Float64,
    "source": pl.Utf8,
}
FLOW_SCHEMA = {
    "market": pl.Utf8,
    "code": pl.Utf8,
    "date": pl.Date,
    "main_net_in": pl.Float64,
    "north_net": pl.Float64,
    "north_hold": pl.Float64,
    "margin_bal": pl.Float64,
    "source": pl.Utf8,
}


def write_financials(db, rows: list[dict]) -> None:
    defaults = {
        "market": "A",
        "code": "600000",
        "report_date": date(2023, 12, 31),
        "published_date": date(2024, 3, 20),
        "roe": 0.2,
        "revenue_yoy": None,
        "profit_yoy": None,
        "dividend_yield": None,
        "market_cap": None,
        "source": "akshare",
    }
    FinancialRepo(db).upsert(
        pl.DataFrame([{**defaults, **row} for row in rows], schema=FIN_SCHEMA)
    )


def write_valuation(db, rows: list[dict]) -> None:
    defaults = {
        "market": "A",
        "code": "600000",
        "date": date(2024, 7, 5),
        "pe_ttm": 18.0,
        "pb": 2.0,
        "dividend_yield": None,
        "market_cap": None,
        "source": "akshare",
    }
    ValuationRepo(db).upsert(
        pl.DataFrame([{**defaults, **row} for row in rows], schema=VAL_SCHEMA)
    )


def write_flow(db, rows: list[dict]) -> None:
    defaults = {
        "market": "A",
        "code": "600000",
        "date": date(2024, 7, 5),
        "main_net_in": 1e8,
        "north_net": None,
        "north_hold": None,
        "margin_bal": None,
        "source": "akshare",
    }
    FundingFlowRepo(db).upsert(
        pl.DataFrame([{**defaults, **row} for row in rows], schema=FLOW_SCHEMA)
    )


def coverage(db, settings, market: str | None = None):
    report = service(db, settings, StubAdapter("akshare", ["A"], sample_bars())).report()
    return report.fundamentals if market is None else report.fundamentals[market]


def test_fundamentals_coverage_counts_codes_and_latest_per_grain(db, settings):
    """三个数各说各的粒度：财务按报告期、估值与资金流按交易日。

    这里最有用的一格是「只数」而不是「行数」：`financials = 400 行` 可能是 200 只票两期，
    而选股扫的是**票**——2 只还是 200 只决定了那一篮子能不能叫「扫描」。
    """
    write_financials(
        db,
        [
            {"code": "600000", "report_date": date(2022, 12, 31)},
            {
                "code": "600000",
                "report_date": date(2023, 12, 31),
                "published_date": date(2024, 4, 1),
            },
            {"code": "600001", "report_date": date(2023, 12, 31)},
        ],
    )
    write_valuation(db, [{"code": "600000"}, {"code": "600001", "date": date(2024, 7, 8)}])
    write_flow(db, [{"code": "600000"}])
    cov = coverage(db, settings, "A")
    assert (cov.financials_codes, cov.valuation_codes, cov.flow_codes) == (2, 2, 1)
    assert cov.financials_latest_period == date(2023, 12, 31)
    assert cov.financials_latest_published == date(2024, 4, 1)
    assert cov.valuation_latest == date(2024, 7, 8)
    assert cov.flow_latest == date(2024, 7, 5)


def test_a_market_never_synced_appears_with_zeros(db, settings):
    """HK/US 一个数都没有时必须在报告里出现：缺席会被读成「这个市场的覆盖不错」，
    而「0 只」才是那条规则真正的输入边界（资金流尤其：港美股根本没有源）。
    """
    write_financials(db, [{"code": "600000"}])
    cov = coverage(db, settings)
    assert set(cov) == {"A", "HK", "US"}
    hk = cov["HK"]
    assert (hk.financials_codes, hk.flow_codes, hk.valuation_latest) == (0, 0, None)


def test_an_unknown_publication_date_does_not_become_the_latest_one(db, settings):
    """NULL 披露日是「源没给」，不是「很久以前披露」：它不能顶掉真正已知的那一天。"""
    write_financials(db, [{"report_date": date(2024, 6, 30), "published_date": None}])
    write_financials(
        db, [{"report_date": date(2023, 12, 31), "published_date": date(2024, 3, 20)}]
    )
    cov = coverage(db, settings, "A")
    assert cov.financials_latest_period == date(2024, 6, 30), "库里最新报告期照实说"
    assert cov.financials_latest_published == date(2024, 3, 20)
