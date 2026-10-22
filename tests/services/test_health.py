from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest
from support import FrozenClock, StubAdapter, sample_bars
from support_valuation import make_container

from sams.core.errors import DatasourceError, RateLimited
from sams.core.notify import FAILED, NO_CHANNEL, SENT, SUPPRESSED
from sams.core.timeutil import CalendarDay, SessionWindow
from sams.datasource.registry import AdapterRegistry
from sams.services.health import KEY_TABLES, HealthService
from sams.services.scheduler.report import ScheduleData
from sams.storage.db import Database
from sams.storage.migrate import apply_migrations, discover
from sams.storage.repos.alerts import Alert, AlertRepo
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.calendar import CalendarRepo
from sams.storage.repos.financials import FinancialRepo, FundingFlowRepo, ValuationRepo
from sams.storage.repos.fx import FxRepo
from sams.storage.repos.ledger import SimAccountRepo, SimOrderRepo
from sams.storage.repos.users import UserRepo
from sams.storage.repos.watchlists import WatchlistRepo

D = date(2024, 1, 2)
PASSWORD = "correct horse battery"


def service(db, settings, *adapters, local_status=None, schedule=None) -> HealthService:
    registry = AdapterRegistry(
        adapters={a.name: a for a in adapters},
        market_sources={"A": tuple(a.name for a in adapters)},
        sleep=lambda seconds: None,
    )
    return HealthService(
        db,
        registry,
        CalendarRepo(db),
        settings,
        local_status=local_status,
        schedule=schedule,
    )


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


# ---------- 通知那一节：四态各几个、本机发得出去吗、有没有工厂不认识的 kind ----------


def alert_with_deliveries(container, *outcomes):
    """一条告警 + 它名下若干次投递。返回那条通道，用例只数健康页给的那几个数。"""
    user = container.users.create("trader", PASSWORD)
    node = container.channels_store.create(
        user.id, "wecom", "主群", {"url": "https://x/y?key=secret-do-not-print"}
    )
    alert = container.alerts_store.create(
        user.id,
        rule_id="r",
        rule_name="PR 低",
        account_id=None,
        account_name="主账户",
        market="A",
        code="601088",
        symbol_name="中国神华",
        severity="mid",
        stage="入池",
        value_text="PR=0.31",
        threshold_text="PR≤0.4",
        action_note="",
        triggered_at=datetime.now(UTC),
        channel_ids=[node.id],
    )
    for outcome in outcomes:
        # no_channel 的意思是「一个通道都没选到」，仓库层拒绝给它配 id
        channel_id = None if outcome == NO_CHANNEL else node.id
        container.alerts_store.record(alert.id, channel_id, outcome, error="errcode=93000")
    return node


def test_notify_section_reports_counts_per_outcome(db, settings):
    container = make_container(db, settings)
    alert_with_deliveries(container, SENT, FAILED)
    report = container.health.report()
    assert report.notify.channels == 1 and report.notify.enabled == 1
    assert (report.notify.sent, report.notify.failed) == (1, 1)
    assert report.notify.suppressed == 0 and report.notify.no_channel == 0
    assert "secret-do-not-print" not in repr(report.notify), "健康页不许把凭据带进报告"


def test_suppressed_and_no_channel_are_counted_separately(db, settings):
    """压制与「没有通道」都是 0 个投递成功，但修法相反：一个改窗口，一个改通道表。"""
    container = make_container(db, settings)
    node = alert_with_deliveries(container, SUPPRESSED, NO_CHANNEL)
    report = container.health.report()
    assert (report.notify.suppressed, report.notify.no_channel) == (1, 1)
    assert (report.notify.sent, report.notify.failed) == (0, 0)
    assert node.is_default


def test_local_status_strings_come_from_the_injected_callable(db, settings):
    """`desktop`/`voice` 那两句话是 monitor 层探测出来的，health 只转述。

    直接在这层 `import sams.monitor.env` 会让 services 长出对 monitor 的依赖
    （守卫不允许，而理由写在 `test_monitor_may_only_reach_core_and_storage`），
    所以这里接一个函数：屏幕上要的那两句从函数里来，函数是谁给的它不问。
    """
    svc = service(
        db,
        settings,
        StubAdapter("akshare", ["A"], sample_bars()),
        local_status=lambda: {"desktop": "远程终端，没有桌面", "voice": ""},
    )
    report = svc.report()
    assert report.notify.desktop == "远程终端，没有桌面"
    assert report.notify.voice == "", "空串就是可用"


def test_a_service_without_local_status_says_not_wired(db, settings):
    """没接入探测时那两个数是 None，不是空串：空串的意思是「试过了，发得出去」。"""
    report = service(db, settings, StubAdapter("akshare", ["A"], sample_bars())).report()
    assert report.notify.desktop is None and report.notify.voice is None


def test_notify_section_is_not_ready_before_the_migration(tmp_path, settings):
    """库里还不是 0015 的形状时，这一节报 `None` 而不是 0，且整份报告不许崩。

    未迁移的库里 `channels`/`notify_log` **已经存在**（它们是 0001 建的），缺的是
    `notify_log.outcome` 那一列——所以「表在不在」不足以决定，还得问列。用一个只到
    0014 的迁移目录来造这个形状，比手工 `DROP COLUMN` 诚实：它验的是「迁移没跑」，
    而列被删掉是另一件事。
    """
    partial = tmp_path / "migrations"
    partial.mkdir()
    for path in discover():
        if not path.name.startswith("0015"):
            (partial / path.name).symlink_to(path)
    fresh = Database.open(tmp_path / "pre-0015.duckdb")
    try:
        apply_migrations(fresh, partial)
        svc = service(fresh, settings, StubAdapter("akshare", ["A"], sample_bars()))
        report = svc.report()
        assert "notify_log" in fresh.table_names(), "前置条件：表在，缺的是那一列"
        assert "outcome" not in fresh.columns("notify_log")
        assert report.notify.channels is None, "None 是「这张表还不是这个样子」，不是 0"
        assert report.notify.sent is None and report.notify.failed is None
        assert any(name.startswith("0015") for name in report.pending_migrations)
    finally:
        fresh.close()


def test_unknown_kind_is_loud(db, settings):
    """库里有一行 `kind='telegram'`（别的版本留下的）时，路由会跳过它——
    健康页必须把那一条数出来，否则「为什么我没收到」会被说成「收到了」。"""
    container = make_container(db, settings)
    user = container.users.create("trader", PASSWORD)
    # 绕过 `ChannelRepo.create`：它按 `KINDS` 校验，而这一行要演的就是校验之前写进去的库
    db.execute(
        "INSERT INTO channels (id, user_id, kind, name, config_encrypted, enabled)"
        " VALUES ('legacy-1', ?, 'telegram', '旧通道', 'not-a-fernet-token', TRUE)",
        [user.id],
    )
    report = container.health.report()
    assert report.notify.unknown_kinds == ("telegram",)
    assert report.notify.channels == 1, "它仍然是一条通道，只是这一版发不出去"


def make_alerts(container, statuses):
    """按给定的状态建几条告警。返回建出来的那些，用例只数健康页给的那几个数。"""
    from datetime import timedelta

    user = container.users.create("trader", PASSWORD)
    made = []
    for index, status in enumerate(statuses):
        alert = container.alerts_store.create(
            user.id,
            rule_id="r",
            rule_name="PR 低",
            account_id=None,
            account_name="主账户",
            market="A",
            code="601088",
            symbol_name="中国神华",
            severity="mid",
            stage="入池",
            value_text="PR=0.31",
            threshold_text="PR≤0.4",
            action_note="",
            # 每条往前推一天：那一列「最早的一条」要有新旧可分
            triggered_at=datetime.now(UTC) - timedelta(days=index),
        )
        if status != "new":
            container.alerts_store.set_status(
                alert.id, status, user_id=user.id, note="测试"
            )
        made.append(container.alerts_store.get(alert.id))
    return made


def test_unhandled_counts_only_the_ones_nobody_has_looked_at(db, settings):
    """`new` 才是积压：handled/dismissed 已经在人的清单上销掉了，再数一遍就是虚报。"""
    container = make_container(db, settings)
    make_alerts(container, ["new", "new", "handled", "dismissed"])

    report = container.health.report()
    assert report.notify.unhandled == 2


def test_an_empty_backlog_is_an_answer_and_not_a_missing_migration(db, settings):
    """0 与 None 是两句话（D93 的同一条线）：0 是好消息，None 是「该跑迁移」。"""
    container = make_container(db, settings)
    assert container.health.report().notify.unhandled == 0


def test_the_oldest_unhandled_is_the_one_that_has_been_waiting_longest(db, settings):
    """积压 40 条与积压 40 天是两个完全不同的运维结论，只靠那个数分不出来。"""
    container = make_container(db, settings)
    made = make_alerts(container, ["new", "new"])
    oldest = min(row.triggered_at for row in made)

    report = container.health.report()
    assert report.notify.unhandled_oldest == oldest


def test_a_dismissed_alert_needs_no_channel_to_leave_the_backlog(db, settings):
    """这条钉的是那两个数的**接缝**：处理动作不碰通道，也不新增投递。"""
    container = make_container(db, settings)
    made = make_alerts(container, ["new"])
    # 通道是这条用例的**对照**而不是道具：断言里 `channels == 1` 才说得清它没被算进积压
    container.channels_store.create(made[0].user_id, "desktop", "本机", {})
    container.alerts_store.set_status(
        made[0].id, "dismissed", user_id=made[0].user_id, note="不需要通道也能销掉"
    )

    report = container.health.report()
    assert report.notify.unhandled == 0 and report.notify.channels == 1
    assert (report.notify.sent, report.notify.failed) == (0, 0)


# ---------- 调度器读数：随报告带上，系统积压单独数 ----------


def test_key_tables_include_the_scheduler_ledgers(db, settings) -> None:
    """两张表各答一个问题，所以都要在这一屏上：`task_runs` 是「每个 job 跑成什么」，
    `scheduler_heartbeats` 是「还在跑吗」（D101：判「它还在不在跑」的读者必须在循环之外）。"""
    report = service(db, settings, StubAdapter("akshare", ["A"], sample_bars())).report()
    assert report.tables["task_runs"] == 0
    assert report.tables["scheduler_heartbeats"] == 0


def test_report_carries_the_schedule_reading(db, settings) -> None:
    """`schedule` 是函数不是调度器对象：这一层调用它、存下结果，不认识循环。

    这里给的是 `collect` 真正会产出的形状（没行就没有年龄）；「从未启动不是过期」
    这一条在 `tests/services/test_scheduler_service.py` 里从 `collect` 那一层钉住。
    """
    data = ScheduleData(heartbeat=None, beat_age_s=None, admins=1, jobs=())
    report = service(
        db, settings, StubAdapter("akshare", ["A"], sample_bars()), schedule=lambda: data
    ).report()
    assert report.schedule is data
    assert report.schedule_stale is False


def test_an_unwired_reader_gives_no_reading_rather_than_a_blank_one(db, settings) -> None:
    """「没接入」是 None，与「接入了但从未启动」（`ScheduleData(heartbeat=None)`）
    是两句话，必须在类型上分得开：把前者显示成后者，运维会去查一个没跑过的调度器。"""
    report = service(db, settings, StubAdapter("akshare", ["A"], sample_bars())).report()
    assert report.schedule is None
    assert report.schedule_stale is False


def make_sys_alert(alerts: AlertRepo, user_id: str, component: str, moment: datetime) -> Alert:
    """一条系统告警的管理员副本：形状照 `has_system_alert` 的 WHERE（`market='SYS'`、
    `rule_id` 为 NULL、`code` 是组件名）。写回来的那一行给用例比时刻用。"""
    return alerts.create(
        user_id,
        rule_id=None,
        rule_name="系统",
        account_id=None,
        account_name="",
        market="SYS",
        code=component,
        symbol_name="",
        severity="mid",
        stage="调度",
        value_text="",
        threshold_text="",
        action_note="",
        triggered_at=moment,
    )


def test_system_alerts_are_not_counted_as_user_backlog(db, settings) -> None:
    """一条系统告警在每个管理员名下各落一行（D102）：把它们数进用户积压，
    「2 个组件在反复告警、2 个管理员」会显示成「未处理 4 条」，而那两句修法是两件事。

    组件数用 `count(DISTINCT code)` 而不是数行：一行管理员副本不是一个新问题。
    四个数都钉（两批的条数与两批各自最早那条）：只钉条数的话，把用户的最早时刻
    填进系统那一格是绿的，而屏幕上那一句会把「一小时前开始的故障」说成「刚发生的」。
    """
    alerts = AlertRepo(db)
    trader = UserRepo(db).create("trader", PASSWORD).id
    keeper = UserRepo(db).create("keeper", PASSWORD).id
    moment = datetime.now(UTC)
    # 系统那一批更早：两批的最早时刻必须是两个不同的数，才有东西可换错
    hour_earlier = moment - timedelta(hours=1)
    user_rows = [
        alerts.create(
            trader,
            rule_id="rule-1",
            rule_name="PR 低",
            account_id=None,
            account_name="主账户",
            market="A",
            code=code,
            symbol_name="某票",
            severity="mid",
            stage="入池",
            value_text="PR=0.31",
            threshold_text="PR≤0.4",
            action_note="",
            triggered_at=moment,
        )
        for code in ("600519", "000001")
    ]
    sys_rows = [
        make_sys_alert(alerts, trader, "sync", hour_earlier),
        make_sys_alert(alerts, keeper, "sync", hour_earlier),
    ]

    report = service(db, settings, StubAdapter("akshare", ["A"], sample_bars())).report()
    assert report.notify.unhandled == 2, "用户积压不混系统副本"
    assert report.notify.unhandled_system == 1, "2 行同组件 = 1 个问题"
    assert report.notify.unhandled_oldest == min(r.triggered_at for r in user_rows)
    assert report.notify.unhandled_system_oldest == min(r.triggered_at for r in sys_rows)


def test_the_system_backlog_uses_the_same_maybe_line_as_the_rest(tmp_path, settings) -> None:
    """未迁移的库里那两个新数也是 None：沿用 `NotifyData` 那一节其余那几个数的同一条线（D93），
    而不是给系统积压另立第二条「该跑迁移」的判据。库的形状照旧用例的办法造：只到 0014。"""
    partial = tmp_path / "migrations"
    partial.mkdir()
    for path in discover():
        if not path.name.startswith("0015"):
            (partial / path.name).symlink_to(path)
    fresh = Database.open(tmp_path / "pre-0015.duckdb")
    try:
        apply_migrations(fresh, partial)
        report = service(fresh, settings, StubAdapter("akshare", ["A"], sample_bars())).report()
        assert report.notify.unhandled_system is None
        assert report.notify.unhandled_system_oldest is None
    finally:
        fresh.close()
