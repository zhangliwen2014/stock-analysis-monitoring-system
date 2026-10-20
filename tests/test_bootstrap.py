from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest
from support import FrozenClock, StubAdapter, sample_bars

from sams.bootstrap import attach_log_sink, build_container
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
            "A": ("akshare", "eastmoney", "tencent", "sina", "akshare_ths"),
            # 链是「按市场的路由」，不是「谁能取日线」：akshare_hk 只报公司行为，
            # 采集时被跳过（D38），备源判据走 registry.chain_for(market, method)
            "HK": ("yfinance", "tencent", "akshare_hk"),
            "US": ("yfinance", "tencent"),
        }
        assert built.registry.chain_for("HK", "daily_bars") == ("yfinance", "tencent")
        assert built.registry.chain_for("HK", "corp_actions") == ("yfinance", "akshare_hk")
        assert built.registry.chain_for("A", "corp_actions") == ("akshare", "akshare_ths")
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
