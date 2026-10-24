from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest
from support import FrozenClock, StubAdapter, sample_bars

import sams.bootstrap as bootstrap
from sams.bootstrap import (
    attach_log_sink,
    build_container,
    build_context,
    build_core,
    build_serve,
)
from sams.core.events import EventBus, SystemAlarm
from sams.monitor.router import NotifyService
from sams.services.accounts import AccountService
from sams.storage.db import Database
from sams.storage.migrate import apply_migrations
from sams.storage.repos.alerts import AlertRepo
from sams.storage.repos.channels import ChannelRepo
from sams.storage.repos.ledger import SimAccountRepo, SimOrderRepo


def stub_adapters() -> dict:
    return {
        a.name: a
        for a in (
            StubAdapter("akshare", ["A"], sample_bars()),
            StubAdapter("eastmoney", ["A"], sample_bars(source="eastmoney")),
            StubAdapter("yfinance", ["US", "HK"], sample_bars("US", "AAPL")),
        )
    }


@pytest.fixture
def container(db, settings):
    clock = FrozenClock(datetime(2026, 3, 9, 4, 0, tzinfo=UTC))
    built = build_container(settings, db=db, clock=clock, adapters=stub_adapters())
    yield built, clock
    built.close()


def test_one_clock_is_shared_across_the_container(container):
    """组装根只造一个时钟：各处各读墙钟，重放时它们会互相不一致。"""
    built, clock = container
    assert built.clock is clock
    assert built.sessions.clock is clock
    assert built.registry.clock is clock
    assert built.health.clock is clock


def test_alarm_reaches_the_log(container, caplog):
    """总线不能是装饰：publish 之后至少要在日志里看得见（spec §12）。"""
    built, _ = container
    with caplog.at_level(logging.ERROR, logger="sams.bootstrap"):
        built.bus.publish(SystemAlarm(component="calendar", message="日历源全挂"))
    assert "日历源全挂" in caplog.text


def test_log_sink_does_not_swallow_other_subscribers():
    bus = EventBus()
    attach_log_sink(bus)
    seen = []
    bus.subscribe(SystemAlarm, seen.append)
    result = bus.publish(SystemAlarm(component="x", message="y"))
    assert result.ok and len(seen) == 1


# ---------- 数据源链的来源：库里有记录就不看配置文件 ----------


@pytest.fixture
def migrated(settings):
    """建好并关闭：下面的用例各自重新打开这个文件库，模拟「两条命令两次进程」。"""
    database = Database.open(settings.db_path)
    apply_migrations(database)
    database.close()
    return settings


def test_registry_chain_comes_from_the_database(migrated):
    built = build_container(migrated)
    try:
        assert built.registry.market_sources == {
            # akshare_ths 只报公司行为（D38）：它在日线链上会被显式跳过，但它确实在库里，
            # 所以 market_sources 这条「链上排了谁」的事实要把它算进来
            "A": ("akshare", "eastmoney", "tencent", "sina", "akshare_ths", "sina_quote"),
            # 链是「按市场的路由」，不是「谁能取日线」：akshare_hk 只报公司行为，
            # 采集时被跳过（D38），备源判据走 registry.chain_for(market, method)
            "HK": ("yfinance", "tencent", "akshare_hk", "sina_quote"),
            "US": ("yfinance", "tencent", "sina_quote"),
        }
        assert built.registry.chain_for("HK", "daily_bars") == ("yfinance", "tencent")
        assert built.registry.chain_for("HK", "corp_actions") == ("yfinance", "akshare_hk")
        assert built.registry.chain_for("A", "corp_actions") == ("akshare", "akshare_ths")
        # 报价链只认 quote 能力：腾讯与新浪报价，日线源一个都不该混进来
        assert built.registry.chain_for("A", "quote") == ("tencent", "sina_quote")
        assert built.registry.chain_for("HK", "quote") == ("tencent", "sina_quote")
        assert built.registry.chain_for("US", "quote") == ("tencent", "sina_quote")
    finally:
        built.close()


def test_disabling_a_source_takes_it_out_of_the_next_container(migrated):
    """管理后台改完不必重启：下一次装配读到的就是新链（M1 的可维护性承诺）。"""
    first = build_container(migrated)
    try:
        first.source_admin.set_enabled("akshare", False)
    finally:
        first.close()

    second = build_container(migrated)
    try:
        assert second.registry.market_sources["A"] == (
            "eastmoney",
            "tencent",
            "sina",
            "akshare_ths",
            "sina_quote",
        )
        assert "akshare" not in second.registry.adapters, "停用源连适配器都不该建"
    finally:
        second.close()


def test_two_rows_can_share_one_implementation(migrated):
    """可插拔的用意：同一实现的第二个实例（不同超时、不同用途）是配置能表达的事。"""
    first = build_container(migrated)
    try:
        first.source_admin.add("tencent_fast", "tencent", ["A"], options={"timeout": 3})
    finally:
        first.close()

    built = build_container(migrated)
    try:
        assert built.registry.market_sources["A"][-1] == "tencent_fast"
        assert built.registry.adapters["tencent_fast"] is not built.registry.adapters["tencent"]
    finally:
        built.close()


def test_options_reach_the_adapter_constructor(migrated):
    """库里存的参数必须真的传进构造函数：静默忽略等于「我以为改了超时，其实没有」。"""
    built = build_container(migrated)
    try:
        built.source_admin.add("sina_short", "sina", ["A"], options={"timeout": 2})
    finally:
        built.close()
    built = build_container(migrated)
    try:
        assert built.registry.adapters["sina_short"]._timeout == 2
    finally:
        built.close()


def test_settings_are_the_fallback_when_the_table_is_empty(migrated):
    """老库（没跑过 0007）也要能装配：这时配置文件说话。"""
    built = build_container(migrated)
    try:
        built.db.execute("DELETE FROM data_source_routes")
        built.db.execute("DELETE FROM data_sources")
    finally:
        built.close()

    second = build_container(migrated)
    try:
        assert second.registry.market_sources["A"] == ("akshare", "eastmoney")
    finally:
        second.close()


def test_a_market_left_without_sources_warns_instead_of_crashing(migrated, caplog):
    """账号管理、健康检查都不该因为「某个市场暂时没源」就跑不起来，但必须说出来。"""
    built = build_container(migrated)
    try:
        built.db.execute("DELETE FROM data_source_routes WHERE market = 'HK'")
    finally:
        built.close()

    with caplog.at_level(logging.WARNING, logger="sams.bootstrap"):
        second = build_container(migrated)
    try:
        assert "HK" in caplog.text
        assert "HK" not in second.registry.market_sources
    finally:
        second.close()


def test_the_container_carries_the_notify_parts(container):
    built, _ = container
    assert isinstance(built.channels_store, ChannelRepo)
    assert isinstance(built.alerts_store, AlertRepo)
    assert isinstance(built.notify, NotifyService)
    assert built.cipher is not None
    # 加解密住在仓库那一层，路由只拿密文/掩码视图：这一句钉住「router 不接 cipher」
    assert not hasattr(built.notify, "cipher")
    # 同一个仓库对象，不是各造一份：两份 `ChannelRepo` 意味着两套对同一条通道的认知
    assert built.notify.channels is built.channels_store
    assert built.notify.alerts is built.alerts_store
    assert built.notify.users is built.users
    # 健康页那两个「本机发得出去吗」的句子来自 monitor：services 不许 import 它，
    # 所以组装根给的是一个函数。没接上的话屏幕上那两格会是 None（未接入探测）。
    assert built.health.local_status == built.notify.local_status


def test_the_sim_repos_are_named_for_what_they_are(container):
    """`accounts` 这个名字属于用户账号服务（`AccountService`），SIM 的两个仓库带 `sim_` 前缀：
    留着裸名，下一个人加字段时会照着 `accounts` 的形状加，于是又造出一个歧义。"""
    built, _ = container
    assert isinstance(built.accounts, AccountService)
    assert isinstance(built.sim_accounts, SimAccountRepo)
    assert isinstance(built.sim_ledger, SimOrderRepo)
    assert not hasattr(built, "orders")


def test_a_real_dispatch_runs_through_the_container(db, settings):
    """容器级的最小一枚：换掉工厂，从 `notify.add` 到 `notify.dispatch` 走一遍。

    路由的分支在 `tests/monitor/test_router.py` 里逐条钉过，这里只验组装根把
    「同一个仓库、同一个总线、被注入的那个工厂」接在了一起——接线错成两个容器时，
    单元用例全绿而屏幕上什么都收不到。
    """
    from sams.core.notify import AlertMessage, NotifyResult

    made: list[str] = []

    def factory(kind, config):
        made.append(kind)

        class Node:
            name = kind

            def available(self):
                return True

            def unavailable_reason(self):
                return ""

            def send(self, message):
                return NotifyResult(ok=True)

        return Node()

    built = build_container(
        settings, db=db, adapters=stub_adapters(), clock=FrozenClock(
            datetime(2026, 3, 9, 4, 0, tzinfo=UTC)
        ), notifier_factory=factory
    )
    try:
        built.users.create("trader", "correct-horse-battery")
        user = built.users.get_by_username("trader")
        node = built.notify.add(user.id, "wecom", "主群", {"url": "https://x/key=k"})
        result = built.notify.dispatch(
            AlertMessage(
                market="A",
                code="601088",
                severity="mid",
                triggered_at=datetime(2026, 3, 9, 3, 0, tzinfo=UTC),
                name="中国神华",
                rule_name="PR 低",
                value_text="PR=0.31",
                threshold_text="PR≤0.4",
            ),
            user_id=user.id,
            channel_ids=[node.id],
        )
        assert result.any_sent
        assert made == ["wecom", "wecom"]  # add 先过一遍工厂，dispatch 再一次
        row = built.alerts_store.get(result.alert_id)
        assert row.severity == "mid"
        assert built.alerts_store.deliveries(result.alert_id)[0].outcome == "sent"
    finally:
        built.close()


def test_the_container_registers_jobs_in_execution_order(container):
    """执行顺序是语义：周复检排在当晚规则之前，汇率排在规则之后、日历之前。

    这里不钉 job 的个数：`stub_adapters()` 只给日线，港美股因此没有基本面 job，
    而那是 D106「按能力注册」要的结果，不是缺数据。
    """
    built, _ = container
    titles = [job.title for job in built.scheduler.jobs]
    assert titles.count("bars@A") == 1
    assert titles.index("recheck@US") < titles.index("rules@US")
    assert titles.index("rules@US") < titles.index("fx@全局")
    assert titles.index("fx@全局") < titles.index("calendar@A")


def test_a_job_alarm_becomes_a_system_alert_for_admins(container):
    """D102：同一条 `SystemAlarm` 在总线有两类订阅者——日志（上面那枚钉的）与管理员。

    路由的分支在 `test_scheduler_alarms.py` 逐条钉过，这里只验接线：告警真的从总线
    走到了 `alerts` 那张表，并带上系统告警那三件标记（`rule_id` 为空、市场是 `SYS`、
    `code` 存组件名）。接线断了的形状是「日志里天天有、管理员一条都没收到」。
    """
    built, _ = container
    built.users.create("root", "correct-horse-battery", is_admin=True)
    admin = built.users.get_by_username("root")

    built.bus.publish(SystemAlarm(component="job.bars.A", message="连续 3 个周期失败"))

    rows = built.alerts_store.list_for(admin.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.market == "SYS" and row.code == "job.bars.A"
    assert row.rule_id is None
    assert "连续 3 个周期失败" in row.payload["extra"]["body"]


def test_health_carries_the_schedule_reading(container):
    """健康页那一节读的就是这里装的那台调度器：两处各造一份时，
    屏幕上「最近一次的结论」说的不是真在跑的那一个。"""
    built, _ = container
    report = built.health.report()
    assert report.schedule is not None
    assert {"bars", "rules", "fx"} <= {job.name for job in report.schedule.jobs}
    assert report.schedule.admins == 0


def test_the_container_carries_the_prune_service(container):
    """三个入口共用一份保留策略：命令行删 5 天、健康页按钮删 7 天，是两种故障。"""
    from sams.services.prune import PruneService

    built, _ = container
    assert isinstance(built.prune, PruneService)


def test_contexts_get_their_own_connection_but_share_the_core(migrated):
    """DuckDB 同进程多连接共享同一个库；连接、事务深度、注册视图都不得跨线程。

    调度器是每条上下文各造一台的对象，但请求队列是进程级的：从第二个上下文入队，
    第一个上下文的那台调度器要能看见——否则 Web 的「立即同步」永远到不了调度线程。
    """
    core = build_core(migrated)
    first = build_context(core)
    second = build_context(core)
    try:
        assert first.db is not second.db
        assert first.registry is core.registry
        assert first.bus is core.bus and first.clock is core.clock
        assert first.scheduler.requests is core.requests
        first.users.create("alice", "correct-horse-battery")
        assert second.users.get_by_username("alice") is not None

        request = second.scheduler.request_run("bars", "A")
        assert first.scheduler.pending_requests == 1
        assert [item.id for item in first.scheduler.requests.drain()] == [request.id]
    finally:
        first.close()
        second.close()
        core.close()


def test_attaching_the_log_sink_twice_still_logs_each_alarm_once(caplog):
    """`serve` 只有一个进程一条总线：重建装配、或 core 与上下文各挂一次，
    同一条系统告警都不该在日志里出现两遍。"""
    bus = EventBus()
    attach_log_sink(bus)
    attach_log_sink(bus)

    with caplog.at_level(logging.ERROR, logger="sams.bootstrap"):
        bus.publish(SystemAlarm(component="dup", message="hello"))

    assert caplog.text.count("hello") == 1


def test_serve_builds_three_contexts_on_one_core(migrated):
    """三条线程各一条连接（D113），但板子、桶与请求队列是同一份。

    板子不是同一个对象时，盘中线程写的那一份 Web 永远看不到；请求队列同理，
    Web 的「立即同步」会入到一个没人读的队列里。
    """
    parts = build_serve(migrated, adapters=stub_adapters())
    try:
        assert len(parts.contexts) == 3
        assert len({id(context.db) for context in parts.contexts}) == 3
        assert all(context.registry is parts.core.registry for context in parts.contexts)
        assert all(context.bus is parts.core.bus for context in parts.contexts)
        assert all(
            context.scheduler.requests is parts.core.requests
            for context in parts.contexts
        )
        assert parts.contexts[1].quotes.board is parts.core.board
        assert parts.contexts[1].intraday.quotes is parts.contexts[1].quotes
        assert parts.runtime.board is parts.core.board
        assert parts.runtime.monitor is parts.contexts[1].intraday
    finally:
        parts.close()


def test_prepare_migrates_before_reading_the_snapshot_table(settings):
    """顺序不能反：`load_board` 读的是 0019 才有的列。

    这一枚把探针挂在 `load_board` 上：先迁移后灌板时它能看见 `quotes_snapshot`，
    反过来则是 `CatalogException`——而那个异常在 `serve` 启动时才炸，最难查。
    """
    parts = build_serve(settings, adapters=stub_adapters())
    quotes = parts.contexts[1].quotes
    original = quotes.load_board
    seen: dict[str, bool] = {}

    def probe() -> int:
        seen["table"] = "quotes_snapshot" in quotes.quotes.db.table_names()
        return original()

    quotes.load_board = probe
    try:
        assert parts.prepare() == 0
        assert seen == {"table": True}
    finally:
        parts.close()


def test_healthz_probes_never_touch_a_thread_context_connection(migrated):
    """`/healthz` 在 HTTP 线程上跑（D113）：调度器的库连接属于调度线程。

    把调度器那个方法换成一炸，`healthz` 仍要给出 ok——说明它走的是注入的探针
    （`FreshScheduleProbe` 每次新开一条连接），而不是手里那台 `Scheduler`。
    """
    parts = build_serve(migrated, adapters=stub_adapters())
    try:
        parts.prepare()
        parts.contexts[0].scheduler.schedule_data = lambda: pytest.fail(
            "healthz 不该借调度线程的连接"
        )

        payload = parts.runtime.healthz()

        assert payload["status"] == "ok"
        assert payload["scheduler"] == {"beat_age_s": None, "stale": False}
        assert payload["db"] == {"ok": True, "error": None}
    finally:
        parts.close()


def test_a_failed_serve_build_closes_the_connections_it_opened(settings, monkeypatch):
    """半成品也要收干净：第二条连接建不起来时，第一条与 core 的连接必须关。

    留着它们时，半成品的句柄会一直握着库文件——Windows 上还会挡住删除与改名，
    而下一次启动的报错只会说「文件被占用」，指不到真正的原因。
    """
    real_context = bootstrap.build_context
    calls = {"n": 0}

    def flaky(core, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("第二条连接建不起来")
        return real_context(core, **kwargs)

    closed: list[int] = []
    real_close = Database.close

    def record_close(self):
        closed.append(id(self))
        real_close(self)

    monkeypatch.setattr(bootstrap, "build_context", flaky)
    monkeypatch.setattr(Database, "close", record_close)

    with pytest.raises(RuntimeError, match="第二条连接建不起来"):
        bootstrap.build_serve(settings, adapters=stub_adapters())

    assert len(closed) == 2, "contexts[0] 与 core 各一条"


def test_build_core_closes_its_own_connection_when_the_registry_fails(settings, monkeypatch):
    """坏掉的数据源记录会让 `_registry` 抛：那条刚开的连接不能留在手里。"""
    def boom(*args, **kwargs):
        raise RuntimeError("坏掉的数据源记录")

    closed: list[int] = []
    real_close = Database.close

    def record_close(self):
        closed.append(id(self))
        real_close(self)

    monkeypatch.setattr(bootstrap, "_registry", boom)
    monkeypatch.setattr(Database, "close", record_close)

    with pytest.raises(RuntimeError, match="坏掉的数据源记录"):
        bootstrap.build_core(settings)

    assert len(closed) == 1


def test_build_context_closes_only_the_connection_it_opened(migrated, monkeypatch):
    """装配炸掉时，自己开的那条要关；调用方传进来的那条绝不能关。"""
    core = bootstrap.build_core(migrated)

    def boom(*args, **kwargs):
        raise RuntimeError("装配炸了")

    closed: list[int] = []
    real_close = Database.close

    def record_close(self):
        closed.append(id(self))
        real_close(self)

    try:
        monkeypatch.setattr(bootstrap, "_context_on", boom)
        monkeypatch.setattr(Database, "close", record_close)

        with pytest.raises(RuntimeError, match="装配炸了"):
            bootstrap.build_context(core)
        assert len(closed) == 1 and closed[0] != id(core.registry_db)

        closed.clear()
        own = Database.open(migrated.db_path)
        with pytest.raises(RuntimeError, match="装配炸了"):
            bootstrap.build_context(core, database=own)
        assert closed == [], "传进来的连接归调用方"
        own.close()
    finally:
        core.close()
