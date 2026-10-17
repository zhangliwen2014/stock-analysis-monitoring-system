"""对抗测试 + 运维可用性回放：本轮（时钟端口 / 三态健康 / SystemAlarm）新增面的破坏性探测。

约定：只用内存/临时库 + 替身，绝不联网；不修改 src/，不改其它测试文件、support.py、conftest.py。
每个用例断言的是**已经成立的行为**：时间一律走注入时钟、过期不可逆、告警只在跨阈值那一次响，
以及运维回放里能稳定复现的那几个坑（health 的口径、CLI 参数缺省）。
"""

from __future__ import annotations

import argparse
import logging
from datetime import UTC, date, datetime

import pytest
from support import FrozenClock, StubAdapter, sample_bars

import sams.cli.health as health_cli
from sams.bootstrap import attach_log_sink, build_container
from sams.core.errors import DatasourceError, RateLimited
from sams.core.events import EventBus, SystemAlarm
from sams.datasource.registry import AdapterRegistry
from sams.services.health import HealthService
from sams.storage.db import Database
from sams.storage.migrate import discover
from sams.storage.repos.calendar import CalendarRepo
from sams.storage.repos.sessions import SessionRepo
from sams.storage.repos.users import UserRepo
from sams.storage.repos.watchlists import WatchlistRepo

PASSWORD = "correct horse battery"
D1, D2 = date(2024, 1, 2), date(2024, 1, 3)


def stub_all(*, akshare_fails: bool = False) -> dict:
    """覆盖 settings 里 A/HK/US 三市场全部源的替身；A 主源可选失败以驱动降级/告警。"""
    ak_outcome = RateLimited("akshare", "429") if akshare_fails else sample_bars(source="akshare")
    return {
        "akshare": StubAdapter("akshare", ["A"], ak_outcome),
        "eastmoney": StubAdapter("eastmoney", ["A"], sample_bars(source="eastmoney")),
        "yfinance": StubAdapter("yfinance", ["US", "HK"], sample_bars("US", "AAPL", "yfinance")),
    }


def _user(db, username="zhangsan") -> str:
    return UserRepo(db).create(username, PASSWORD).id


# --------------------------------------------------------------------------- PART 1


def test_session_at_exact_ttl_is_rejected_boundary_is_exclusive(db):
    """边界：恰好在 TTL 时刻，会话被判定过期（`>` 而非 `>=`）。可复现，但文档没写这个方向。"""
    clock = FrozenClock(datetime(2030, 1, 1, tzinfo=UTC))
    repo = SessionRepo(db, clock)
    token = repo.issue(_user(db), ttl_days=5)  # expires_at = 2030-01-06 00:00

    clock.advance(days=5)
    assert repo.verify(token) is None, "恰好到期应判过期（exclusive），代码与之一致"

    clock.advance(seconds=-1)
    assert repo.verify(token) is not None, "到期前一秒仍有效"


def test_clock_rewind_does_not_extend_or_resurrect_a_session(db):
    """BUG（故意失败）：过期判定没有下界。时钟回拨后，已过期会话重新可用。

    verify 只比 `expires_at > now`，没有 not-before/高水位；系统时钟被 NTP 往回拨一步
    或回放里把时钟倒退，都会复活/延长会话。断言的是本轮想要的「过期不可逆」。
    """
    clock = FrozenClock(datetime(2030, 1, 1, tzinfo=UTC))
    repo = SessionRepo(db, clock)
    token = repo.issue(_user(db), ttl_days=14)  # expires_at = 2030-01-15

    clock.advance(days=20)  # 2030-01-21：按注入时钟已过期
    assert repo.verify(token) is None

    clock.advance(days=-30)  # 回拨到 2029-12-22
    assert repo.verify(token) is None, "时钟回拨不应让已过期会话复活"


def test_active_count_and_verify_share_the_injected_clock(db):
    """issue/verify/active_count/revoke 全部走注入时钟：

    计数若还用 SQL now()，用未来时钟签会话、再按时钟判定过期后，verify 说失效、
    active_count 却仍算「活跃」→ disable/passwd 打印的「吊销会话 N 个」跨进程/回放不可复现。
    """
    clock = FrozenClock(datetime(2030, 1, 1, tzinfo=UTC))
    repo = SessionRepo(db, clock)
    uid = _user(db)
    token = repo.issue(uid, ttl_days=14)

    clock.advance(days=20)  # 注入时钟判定已过期
    assert repo.verify(token) is None

    # 活跃计数与会话过期判定共用同一个注入时钟：两者背离时，「还能登录」和「会话数」会互相打脸
    assert repo.active_count(uid) == 0, "active_count 必须跟着注入时钟走"


def test_revoke_stamps_revoked_at_with_the_injected_clock(db):
    """审计写入也走注入时钟：回放里这条时间戳不该变成「今天」。"""
    clock = FrozenClock(datetime(2030, 1, 1, tzinfo=UTC))
    repo = SessionRepo(db, clock)
    token = repo.issue(_user(db))
    clock.advance(days=3)
    repo.revoke(token)
    revoked_at = db.query("SELECT revoked_at FROM user_sessions")["revoked_at"][0]
    assert revoked_at is not None
    assert revoked_at.astimezone(UTC).date() == date(2030, 1, 4), (
        f"revoked_at 应跟着注入时钟：{revoked_at}"
    )


def test_token_is_bound_to_the_user_it_was_issued_for(db):
    """用户隔离：令牌只认签发用户；吊销其一不影响另一。"""
    repo = SessionRepo(db)
    u1, u2 = _user(db, "alice"), _user(db, "bob")
    t1, t2 = repo.issue(u1), repo.issue(u2)
    assert repo.verify(t1) == u1 and repo.verify(t2) == u2
    repo.revoke(t1)
    assert repo.verify(t1) is None
    assert repo.verify(t2) == u2, "吊销 A 的会话不得波及 B"
    assert repo.active_count(u1) == 0 and repo.active_count(u2) == 1


# --------------------------------------------------------------------------- PART 2


def test_system_alarm_fires_exactly_once_at_threshold_and_reaches_log(caplog):
    """三源失败的第 3 次发 SystemAlarm（==3，只一次），并经 attach_log_sink 落到日志。"""
    clock = FrozenClock(datetime(2030, 1, 1, tzinfo=UTC))
    bus = EventBus()
    attach_log_sink(bus)
    alarms: list[SystemAlarm] = []
    bus.subscribe(SystemAlarm, alarms.append)

    reg = AdapterRegistry(
        adapters={"akshare": StubAdapter("akshare", ["A"], RateLimited("akshare", "429"))},
        market_sources={"A": ("akshare",)},
        sleep=lambda _s: None,
        bus=bus,
        clock=clock,
    )
    with caplog.at_level(logging.ERROR, logger="sams.bootstrap"):
        for _ in range(4):  # 第 4 次不得再告警
            with pytest.raises(DatasourceError):
                reg.fetch_daily("A", "600519", D1, D1)

    assert len(alarms) == 1, "只有跨过阈值那一次才告警"
    assert "[alarm]" in caplog.text and "datasource:akshare" in caplog.text
    # 事件时间戳也走注入时钟：回放里「源挂了」必须记在回放的那个时刻
    assert alarms[0].ts == datetime(2030, 1, 1, tzinfo=UTC)


def test_handler_exception_does_not_break_other_subscribers():
    """EventBus 逐 handler 隔离：一个订阅者抛异常，其它仍收到，结果标记 not ok。绿（无中断）。"""
    bus = EventBus()

    def boom(_event):
        raise RuntimeError("sink down")

    seen = []
    bus.subscribe(SystemAlarm, boom)
    bus.subscribe(SystemAlarm, seen.append)
    result = bus.publish(SystemAlarm(component="x", message="y"))
    assert len(seen) == 1, "坏订阅者不得阻断后面的订阅者"
    assert not result.ok


def test_rebuilding_container_on_same_bus_double_subscribes_the_log_sink(db, settings, caplog):
    """已知边界：build_container 每次都往传入的 bus 上再挂一个 log sink，复用同一 bus 会重复告警行。

    CLI 单进程只 build 一次，不受影响；常驻进程若复用 bus 需自己只挂一次 sink。
    """
    bus = EventBus()
    build_container(settings, db=db, bus=bus, adapters=stub_all())
    build_container(settings, db=db, bus=bus, adapters=stub_all())
    with caplog.at_level(logging.ERROR, logger="sams.bootstrap"):
        bus.publish(SystemAlarm(component="dup", message="hello"))
    assert caplog.text.count("hello") == 2, "同一 bus 上 sink 被挂了两次"


def test_health_service_survives_unmigrated_database(tmp_path, settings):
    """健康检查在未迁移库上不崩：tables/calendar 空、pending 列全部迁移名。"""
    fresh = Database.open(tmp_path / "nope.duckdb")
    try:
        svc = HealthService(
            fresh,
            AdapterRegistry(adapters={}, market_sources={}, sleep=lambda _s: None),
            CalendarRepo(fresh),
            settings,
        )
        report = svc.report()
        assert report.tables == {} and report.calendar_years == {}
        assert report.pending_migrations == [p.name for p in discover()]
    finally:
        fresh.close()


def test_health_show_exit_codes(db, settings, tmp_path, capsys):
    """退出码矩阵：全未知→0；降级→1；有未应用迁移→1（并打印库路径 + 迁移名）。"""
    # 全未知（从未调用）：非降级、无待迁移 → 0
    fresh_ok = build_container(settings, db=db, adapters=stub_all())
    assert health_cli.show(fresh_ok, argparse.Namespace()) == 0
    out = capsys.readouterr().out
    assert "未知（未调用）" in out and "尚未被调用过" in out

    # 降级源：akshare 失败、eastmoney 接手 → A 仍取到数，但 akshare 记为故障 → 1
    deg = build_container(settings, db=db, adapters=stub_all(akshare_fails=True))
    deg.registry.sleep = lambda _s: None
    deg.registry.fetch_daily("A", "600519", D1, D2)
    assert health_cli.show(deg, argparse.Namespace()) == 1
    out = capsys.readouterr().out
    assert "失败×1" in out and "未恢复，检查网络或换源" in out

    # 未迁移库：pending 非空 → 1，且报告里点名 DB 路径与迁移文件名
    unmigrated = Database.open(tmp_path / "pending.duckdb")
    try:
        pend = build_container(settings, db=unmigrated, adapters=stub_all())
        assert health_cli.show(pend, argparse.Namespace()) == 1
        out = capsys.readouterr().out
        assert str(unmigrated.path) in out and "待应用迁移" in out and "0001_init.sql" in out
        pend.close()
    finally:
        unmigrated.close()


# --------------------------------------------------------------------------- PART 4


def test_watchlist_repo_never_leaks_across_users(db):
    """D5 隔离：常规读只按 user_id；list_all 是显式 admin 审计视图。"""
    w = WatchlistRepo(db)
    w.add("u1", "A", "600519")
    w.add("u2", "A", "000001")
    w.add("u2", "US", "AAPL")
    assert set(w.codes("u1")) == {("A", "600519")}
    assert set(w.codes("u2")) == {("A", "000001"), ("US", "AAPL")}
    assert sorted(i.user_id for i in w.list_all()) == ["u1", "u2", "u2"]


def test_sync_through_real_registry_is_idempotent_and_overlap_safe(db, settings):
    """经真实 AdapterRegistry（非 StubRegistry）跑两遍同区间行数不变；重叠区间共享日不重复。"""
    built = build_container(settings, db=db, adapters=stub_all())
    built.registry.sleep = lambda _s: None

    built.sync.sync_daily("A", "600519", D1, D2)
    built.sync.sync_daily("A", "600519", D1, D2)
    assert built.bars.count("A", "600519") == 2

    # 换一批数据、区间与已有 03-03 重叠：应替换 03 而非复制，得到 02/03/04 三行
    built.registry.adapters["akshare"].outcome = sample_bars(start=date(2024, 1, 3))  # 03,04
    built.sync.sync_daily("A", "600519", date(2024, 1, 3), date(2024, 1, 4))
    stored = built.bars.load("A", ["600519"])
    assert built.bars.count("A", "600519") == 3
    assert stored["date"].to_list() == [D1, D2, date(2024, 1, 4)]
