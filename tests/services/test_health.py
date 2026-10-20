from __future__ import annotations

from datetime import UTC, date, datetime

import polars as pl
import pytest
from support import FrozenClock, StubAdapter, sample_bars

from sams.core.errors import DatasourceError, RateLimited
from sams.core.timeutil import CalendarDay, SessionWindow
from sams.datasource.registry import AdapterRegistry
from sams.services.health import KEY_TABLES, HealthService
from sams.storage.db import Database
from sams.storage.migrate import discover
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.calendar import CalendarRepo
from sams.storage.repos.financials import FinancialRepo, FundingFlowRepo, ValuationRepo
from sams.storage.repos.fx import FxRepo
from sams.storage.repos.ledger import SimAccountRepo, SimOrderRepo
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


# ---------- 账本那一节：能不能记账、能不能拆换汇（M3-A 的 U26 之后） ----------


def ledger_service(
    db, settings, *, sim_accounts=None, sim_ledger=None, fx=None, today=D
) -> HealthService:
    """把三个仓库接上，时钟冻在 `today`：`age_days` 不冻住就没法断言。"""
    registry = AdapterRegistry(
        adapters={"akshare": StubAdapter("akshare", ["A"], sample_bars())},
        market_sources={"A": ("akshare",)},
        sleep=lambda seconds: None,
    )
    return HealthService(
        db,
        registry,
        CalendarRepo(db),
        settings,
        clock=FrozenClock(datetime(today.year, today.month, today.day, 15, tzinfo=UTC)),
        sim_accounts=sim_accounts,
        sim_ledger=sim_ledger,
        fx=fx,
    )


def test_the_ledger_section_counts_accounts_orders_and_open_positions(db, settings) -> None:
    """三个数分开报：只有订单没有持仓（全卖了）与一笔都没记，是两件不同的事。

    持仓只数**不含清仓后留下的 0 股行**——那一行是历史，不是「还持有」。
    """
    accounts = SimAccountRepo(db)
    ledger = SimOrderRepo(db, accounts)
    user_id = UserRepo(db).create("trader", PASSWORD).id
    account = accounts.open(user_id, "主账户")
    accounts.adjust_cash(account.id, "CNY", 100_000.0, reason="初始", actor_id=user_id)
    ledger.record_fill(
        account_id=account.id,
        user_id=user_id,
        market="A",
        code="600000",
        side="buy",
        qty=100,
        price=40.0,
        currency="CNY",
    )
    report = ledger_service(
        db, settings, sim_accounts=accounts, sim_ledger=ledger, fx=FxRepo(db)
    ).report()
    assert (report.ledger.accounts, report.ledger.orders, report.ledger.positions) == (1, 1, 1)


def test_a_closed_position_is_not_counted_as_a_holding(db, settings) -> None:
    accounts = SimAccountRepo(db)
    ledger = SimOrderRepo(db, accounts)
    user_id = UserRepo(db).create("trader", PASSWORD).id
    account = accounts.open(user_id, "主账户")
    accounts.adjust_cash(account.id, "CNY", 100_000.0, reason="初始", actor_id=user_id)
    for side, qty in (("buy", 100.0), ("sell", 100.0)):
        ledger.record_fill(
            account_id=account.id,
            user_id=user_id,
            market="A",
            code="600000",
            side=side,
            qty=qty,
            price=40.0,
            currency="CNY",
        )
    report = ledger_service(
        db, settings, sim_accounts=accounts, sim_ledger=ledger, fx=FxRepo(db)
    ).report()
    assert (report.ledger.orders, report.ledger.positions) == (2, 0)


def test_fx_freshness_is_reported_per_pair_not_as_one_global_max(db, settings) -> None:
    """两个币种对各报自己的最后一天：全局 `max(date)` 会说「汇率很新」。

    而 USD→CNY 昨天刚同步、HKD→CNY 三周没动时，港币那笔持仓的换汇影响照样是假数——
    一个数替两个对背书，屏幕上看不出的那一半正是错的那一半。
    """
    fx = FxRepo(db)
    fx.put(date(2024, 1, 2), "USD", "CNY", 7.1, source="yfinance")
    fx.put(date(2023, 12, 1), "HKD", "CNY", 0.91, source="manual")
    pairs = ledger_service(db, settings, fx=fx, today=D).report().ledger.fx
    by_pair = {(p.base, p.quote): p for p in pairs}
    assert by_pair[("USD", "CNY")].age_days == 0
    assert by_pair[("USD", "CNY")].source == "yfinance"
    assert by_pair[("HKD", "CNY")].age_days == 32
    assert by_pair[("HKD", "CNY")].latest == date(2023, 12, 1)


def test_an_empty_fx_table_is_an_empty_list_not_a_missing_section(db, settings) -> None:
    """没同步过要报「一个币种对都没有」，不是这一节不出现：缺席会被读成「账本没在用」。"""
    report = ledger_service(db, settings, fx=FxRepo(db)).report()
    assert report.ledger.fx == ()


def test_the_ledger_section_survives_a_database_without_migration_0014(tmp_path, settings) -> None:
    """0014 之前的老库：那三个数是 0，而不是当场 `Catalog Error`。

    崩在这里的症状比缺数据严重：`sams health` 正是「先看看这个库怎么了」的入口，
    而待迁移的库恰好是它最该开口说话的那一刻——它自己不能是第一个倒下的。
    """
    fresh = Database.open(tmp_path / "pre-0014.duckdb")
    try:
        accounts = SimAccountRepo(fresh)
        report = ledger_service(
            fresh,
            settings,
            sim_accounts=accounts,
            sim_ledger=SimOrderRepo(fresh, accounts),
            fx=FxRepo(fresh),
        ).report()
        assert (report.ledger.accounts, report.ledger.orders) == (0, 0)
        assert report.ledger.positions == 0 and report.ledger.fx == ()
        # 前置条件也说一句：这里数不到东西是因为迁移还没跑，不是因为账本真的空
        assert any(name.startswith("0014") for name in report.pending_migrations)
    finally:
        fresh.close()
