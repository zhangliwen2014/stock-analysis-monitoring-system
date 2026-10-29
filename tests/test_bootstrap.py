from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import polars as pl
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
from sams.core.errors import StorageError, ValidationError
from sams.core.events import EventBus, SystemAlarm
from sams.core.quotes import Quote
from sams.monitor.queue import QueuedAlertSender
from sams.monitor.router import NotifyService
from sams.services.accounts import AccountService
from sams.services.intraday import MarketPoll
from sams.services.quotes import QuotePollResult
from sams.storage.db import Database
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


def test_registry_chain_comes_from_the_database(migrated):
    built = build_container(migrated)
    try:
        assert built.registry.market_sources == {
            # akshare_ths 只报公司行为（D38）：它在日线链上会被显式跳过，但它确实在库里，
            # 所以 market_sources 这条「链上排了谁」的事实要把它算进来
            "A": (
                "akshare",
                "eastmoney",
                "tencent",
                "sina",
                "akshare_ths",
                "eastmoney_f10",
                "sina_quote",
            ),
            # 链是「按市场的路由」，不是「谁能取日线」：akshare_hk 只报公司行为，
            # 采集时被跳过（D38），备源判据走 registry.chain_for(market, method)
            "HK": (
                "yfinance",
                "tencent",
                "akshare_hk",
                "eastmoney_f10",
                "eastmoney_hk_f10",
                "sina_quote",
            ),
            "US": ("yfinance", "tencent", "sina_quote"),
        }
        assert built.registry.chain_for("HK", "daily_bars") == ("yfinance", "tencent")
        assert built.registry.chain_for("HK", "corp_actions") == ("yfinance", "akshare_hk")
        assert built.registry.chain_for("A", "corp_actions") == ("akshare", "akshare_ths")
        # 报价链只认 quote 能力：腾讯与新浪报价，日线源一个都不该混进来
        assert built.registry.chain_for("A", "quote") == ("tencent", "sina_quote")
        assert built.registry.chain_for("HK", "quote") == ("tencent", "sina_quote")
        assert built.registry.chain_for("US", "quote") == ("tencent", "sina_quote")
        # F10（D214）：US 的链上没有概况/公告源；港股财务多一个只服务它的源
        assert built.registry.chain_for("A", "company_profile") == ("eastmoney_f10",)
        assert built.registry.chain_for("HK", "announcements") == ("eastmoney_f10",)
        assert built.registry.chain_for("US", "company_profile") == ()
        assert built.registry.chain_for("HK", "financials") == ("eastmoney_hk_f10",)
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
            "eastmoney_f10",
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


def test_the_container_carries_a_minute_reader(container):
    """分钟服务住 `ServiceContext`：一条线程一份，与 `analysis` 同一侧（D163）。

    这里只钉标签不钉 `ok`：`stub_adapters()` 那三个替身没有分钟方法，
    可用性当场就是 False，而装配错了时红的是「没有 minute 这一格」。
    """
    built, _clock = container
    assert built.minute.availability("A").bars.label == "分钟 K"
    assert built.minute.availability("A").series.label == "分时"


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


def test_the_manual_prune_runner_hands_back_the_connection_it_opened(migrated, monkeypatch):
    """一次调用一条连接，用完就关：漏关的那一条会一直握着库文件。

    握着库文件不只是占内存：Windows 上它挡住删除与改名，而 `db prune` 的运维路径
    正是「清理完把这库挪走」。
    """
    opened: list[Database] = []
    real_open = Database.open

    def spy(path):
        database = real_open(path)
        opened.append(database)
        return database

    monkeypatch.setattr(Database, "open", staticmethod(spy))
    report = bootstrap.FreshPruneRunner(migrated).run(dry_run=True)

    assert report.dry_run is True and report.steps
    assert len(opened) == 1, "手动清理借来的不该只有一条"
    with pytest.raises(StorageError, match="SQL 执行失败"):
        opened[0].scalar("SELECT 1")


def test_serve_builds_four_contexts_on_one_core(migrated):
    """四条线程各一条连接（D113），但板子、桶与请求队列是同一份。

    板子不是同一个对象时，盘中线程写的那一份 Web 永远看不到；请求队列同理，
    Web 的「立即同步」会入到一个没人读的队列里。
    """
    parts = build_serve(migrated, adapters=stub_adapters())
    try:
        core, contexts, runtime = parts.core, parts.contexts, parts.runtime
        assert len(contexts) == 4
        assert len({id(ctx.db) for ctx in contexts}) == 4
        assert contexts[0].registry is contexts[1].registry
        assert contexts[0].bus is contexts[1].bus
        assert contexts[0].scheduler.requests is contexts[1].scheduler.requests
        assert contexts[1].quotes.board is core.board
        assert contexts[1].intraday.quotes is contexts[1].quotes
        assert contexts[1].intraday.on_poll is not None
        assert contexts[0].intraday.on_poll is None
        assert runtime.board is core.board
        assert runtime.monitor is contexts[1].intraday
        assert runtime.worker is not None
        # 手动清理不许借维护线程那条连接（D113）：`Database._tx_depth` 没有锁，两次
        # 清理共用一条连接时先结束的那边会把另一边正在写的事务一起 COMMIT 掉
        assert runtime.manual_prune is not None
        assert runtime.manual_prune is not runtime.prune
    finally:
        parts.close()


def test_only_the_intraday_context_sends_through_the_queue(migrated):
    """只有盘中那条走队列：调度线程的告警照旧直发（§5.5）。

    两条都入队时，`serve` 的停机顺序一变，调度侧的告警就排在没人读的队列里。
    """
    parts = build_serve(migrated, adapters=stub_adapters())
    try:
        scheduler_ctx, intraday_ctx, notify_ctx, maintenance_ctx = parts.contexts
        assert isinstance(intraday_ctx.rule_engine.sender, QueuedAlertSender)
        assert not isinstance(scheduler_ctx.rule_engine.sender, QueuedAlertSender)
    finally:
        parts.close()


def test_every_context_shares_the_core_version_gate(migrated):
    """写面的 bump 与盘中缓存的读闸必须是同一份闸，否则 §5.4 整条链是装饰。

    每份上下文各造一个 `VersionGate` 时，调度线程写的日线 bump 在自己那份上响，
    盘中线程那份永远不动——症状不是报错，是盘中拿着昨天的复权价判今天的规则。
    """
    parts = build_serve(migrated, adapters=stub_adapters())
    try:
        for context in parts.contexts:
            assert context.rule_service.versions is parts.core.versions
            assert context.rule_deriver.versions is parts.core.versions
            assert context.theses.versions is parts.core.versions
            assert context.ledger.versions is parts.core.versions
        # job 的 deps 不在 Scheduler 上露出来，它是每个 JobSpec 那个 partial 的第一个实参
        deps = parts.contexts[0].scheduler.jobs[0].run.args[0]
        assert deps.versions is parts.core.versions
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


def test_build_core_closes_its_own_connection_when_the_cipher_fails(migrated, monkeypatch):
    """密钥配坏了也是装配失败：`cipher_for` 在 return 里求值时，那条连接就漏了。"""
    def boom(*args, **kwargs):
        raise ValidationError("secret_key 非法")

    closed: list[int] = []
    real_close = Database.close

    def record_close(self):
        closed.append(id(self))
        real_close(self)

    monkeypatch.setattr(bootstrap, "cipher_for", boom)
    monkeypatch.setattr(Database, "close", record_close)

    with pytest.raises(ValidationError, match="secret_key"):
        bootstrap.build_core(migrated)

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


def test_build_container_closes_its_own_connection_when_assembly_fails(settings, monkeypatch):
    """`build_container` 自己开的那条：后面三步任何一步炸了都要关。"""
    def boom(*args, **kwargs):
        raise RuntimeError("装配炸了")

    closed: list[int] = []
    real_close = Database.close

    def record_close(self):
        closed.append(id(self))
        real_close(self)

    monkeypatch.setattr(bootstrap, "build_core", boom)
    monkeypatch.setattr(Database, "close", record_close)

    with pytest.raises(RuntimeError, match="装配炸了"):
        bootstrap.build_container(settings)

    assert len(closed) == 1


def test_build_container_never_closes_a_caller_connection(db, settings, monkeypatch):
    """镜像 `test_build_context_closes_only_the_connection_it_opened`：传进来的不归这里关。"""
    def boom(*args, **kwargs):
        raise RuntimeError("装配炸了")

    closed: list[int] = []
    real_close = Database.close

    def record_close(self):
        closed.append(id(self))
        real_close(self)

    monkeypatch.setattr(bootstrap, "build_core", boom)
    monkeypatch.setattr(Database, "close", record_close)

    with pytest.raises(RuntimeError, match="装配炸了"):
        bootstrap.build_container(settings, db=db)

    assert closed == []


def test_serve_parts_close_tries_every_step_and_reraises_the_first():
    """一条连接关不掉不该替另外三条做决定：少关一条就是多泄漏一条。"""
    closed: list[str] = []

    class Part:
        def __init__(self, name: str, *, boom: bool = False) -> None:
            self.name = name
            self.boom = boom

        def close(self) -> None:
            closed.append(self.name)
            if self.boom:
                raise RuntimeError(f"{self.name} 关不掉")

    parts = bootstrap.ServeParts(
        core=Part("core"),
        contexts=(Part("context-0", boom=True), Part("context-1"), Part("context-2")),
        runtime=object(),
    )

    with pytest.raises(RuntimeError, match="context-0 关不掉"):
        parts.close()

    assert closed == ["context-0", "context-1", "context-2", "core"]


def test_an_intraday_hit_flows_from_the_board_to_the_queue(migrated):
    """全链一趟：板 → 时段/宇宙闸 → 引擎 → 队列 → 通知线程 → 收件箱。

    每一段单独都有用例，串起来这段没人跑过：`on_poll` 没接进 `IntradayMonitor`、
    队列没接到 `NotifyWorker`、或者 `prepare()` 的迁移少了一列，都在这一枚里现形。
    """
    parts = build_serve(migrated, adapters=stub_adapters())
    try:
        parts.prepare()
        core = parts.core
        intraday_ctx = parts.contexts[1]
        notify_ctx = parts.contexts[2]

        user = intraday_ctx.users.create("trader", "correct-horse-battery")
        intraday_ctx.rules_store.create(
            user.id,
            kind="stop.fixed",
            name="跌破 10 元",
            target_type="symbol",
            target_ref="A/601088",
            params={"price": 10.0},
        )

        # 「今天」跟着报价的市场本地日走（引擎读日线就按它）：机器时区若是 UTC，
        # date.today() 与上海日可能差一天，日线就会落在 as_of 之外
        moment = datetime.now(UTC)
        today = moment.astimezone(ZoneInfo("Asia/Shanghai")).date()
        bars = pl.DataFrame(
            {
                "market": ["A"] * 3,
                "code": ["601088"] * 3,
                "date": [today - timedelta(days=2), today - timedelta(days=1), today],
                "open": [10.6, 10.5, 10.05],
                "high": [10.8, 10.6, 10.1],
                "low": [10.2, 10.1, 9.9],
                "close": [10.5, 10.4, 9.9],
                "volume": [1e6] * 3,
                "amount": [1e7] * 3,
                "source": ["tencent"] * 3,
            }
        )
        intraday_ctx.rule_data.bars.upsert(bars)
        # 复权口径要有人确认过：裸价区间没有覆盖确认时 `AnalysisService` 直接拒读
        # （那句「0 行既可能是没分红，也可能是没查过」），盘中这一趟就什么都判不出
        intraday_ctx.corp_actions_coverage.record(
            "A",
            "601088",
            covered_from=today - timedelta(days=2),
            covered_to=today,
            full_history=True,
            src="tencent",
        )

        quote = Quote(
            market="A",
            code="601088",
            ts=moment - timedelta(seconds=5),
            price=9.4,
            open=9.8,
            prev_close=10.2,
            source="tencent",
        )
        core.board.update("A", (quote,), now=moment)

        # 直接灌一轮结果：poll_once 的时段/宇宙闸在 Task 10 之前的用例里盖过了
        got = intraday_ctx.intraday.on_poll(
            (
                MarketPoll(
                    "A",
                    result=QuotePollResult(
                        quotes=(quote,), skipped=(), errors=(), sources=()
                    ),
                ),
            )
        )
        assert got.hits == 1
        assert core.alerts_queue.qsize() == 1

        rows = intraday_ctx.alerts_store.list_for(user.id)
        assert len(rows) == 1
        row = rows[0]
        assert row.stage == "urgent"  # 开 9.8 < 线 10 ≤ 昨收 10.2：跳空破位
        assert row.intraday is True
        assert "报价" in row.payload["extra"]["body"]

        stop = threading.Event()
        stop.set()
        parts.runtime.worker.run(stop=stop)  # 同步排水：工作单全部跑完
        assert core.alerts_queue.empty()
        deliveries = notify_ctx.alerts_store.deliveries(row.id)
        assert deliveries and deliveries[0].outcome == "no_channel"  # 没配通道，落了账
    finally:
        parts.close()


def test_container_wires_annotations(container):
    """批注仓库与服务必须进同一条线程上下文（D113）：Web 线程不能借用别人的连接。"""
    built, _ = container
    user = built.users.create("writer", "correct-horse-battery")
    made = built.annotations.create(
        user.id, target_type="symbol", target_ref="A:600519", body_md="先看图"
    )
    assert built.annotations_store.get(made.id) is not None
    rows = built.annotations.list_target(user.id, "symbol", "A:600519")
    assert [row.id for row in rows] == [made.id]


def test_container_wires_attachments_and_the_pruner(container):
    """附件根与库同目录：备份时复制两样东西就够，不需要再记第二个位置。"""
    from io import BytesIO
    from pathlib import Path

    from PIL import Image

    built, _ = container
    user = built.users.create("writer", "correct-horse-battery")
    note = built.annotations.create(
        user.id, target_type="symbol", target_ref="A:600519", body_md="先看图"
    )
    buffer = BytesIO()
    Image.new("RGBA", (8, 8), (1, 2, 3, 4)).save(buffer, format="PNG")
    row = built.attachments.add(user.id, note.id, filename="x.png", data=buffer.getvalue())
    root = Path(built.settings.db_path).parent / "attachments"
    assert (root / row.storage_path).is_file()
    assert built.attachments.resolve(user.id, note.id, row.id).id == row.id
    assert built.attachments_store.get(row.id) is not None
    assert built.prune.annotations is built.annotation_pruner


def test_container_wires_the_push_context_and_the_review(container):
    """provider 只有一格接得住队列与重发：`notify.context_provider`（§7.3）。

    这枚钉的是装配本身的接缝：`AnnotationContextProvider` 改了构造签名而这里没跟上，
    红的是这一条，而不是某个交易日里通知线程的栈追踪。
    """
    built, _ = container
    assert built.annotation_context is not None
    assert built.notify.context_provider is built.annotation_context
    assert built.review is not None
    assert built.app_settings.team_sharing() is True
