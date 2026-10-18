from __future__ import annotations

from datetime import UTC, date, datetime

import polars as pl
import pytest
from support import FrozenClock

from sams.core.clock import SYSTEM_CLOCK
from sams.core.errors import DatasourceError, EmptyResponse, FieldDrift, RateLimited, TransientError
from sams.core.events import EventBus, SystemAlarm
from sams.datasource.actions import empty_actions
from sams.datasource.base import BAR_COLUMNS
from sams.datasource.ratelimit import TokenBucket
from sams.datasource.registry import AdapterRegistry
from sams.storage.repos.source_health import SourceHealthRepo

D1, D2 = date(2024, 1, 2), date(2024, 1, 3)


def frame(source: str) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "market": ["A", "A"],
            "code": ["600519", "600519"],
            "date": [D1, D2],
            "open": [1.0, 2.0],
            "high": [1.5, 2.5],
            "low": [0.5, 1.5],
            "close": [1.2, 2.2],
            "volume": [100.0, 200.0],
            "amount": [120.0, 440.0],
            "source": [source, source],
        }
    )


class FakeAdapter:
    """按脚本应答：outcomes 里每一项要么是返回帧，要么是要抛的异常。"""

    def __init__(self, name: str, markets=("A",), *outcomes) -> None:
        self.name = name
        self.markets = frozenset(markets)
        self.outcomes = list(outcomes)
        self.calls = 0

    def daily_bars(self, market, code, start, end):
        self.calls += 1
        outcome = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def registry(
    *adapters, markets=("A",), store=None, bus=None, clock=SYSTEM_CLOCK
) -> AdapterRegistry:
    by_name = {a.name: a for a in adapters}
    return AdapterRegistry(
        adapters=by_name,
        market_sources={m: tuple(by_name) for m in markets},
        sleep=lambda seconds: None,
        store=store,
        bus=bus,
        clock=clock,
    )


def test_primary_success_is_not_degraded():
    reg = registry(FakeAdapter("akshare", ["A"], frame("akshare")))
    result = reg.fetch_daily("A", "600519", D1, D2)
    assert result.source == "akshare"
    assert not result.degraded
    assert result.frame.columns == BAR_COLUMNS
    assert reg.health()[0].ok and reg.health()[0].consecutive_failures == 0


def test_rate_limited_primary_falls_back_and_is_recorded():
    primary = FakeAdapter("akshare", ["A"], RateLimited("akshare", "429"))
    backup = FakeAdapter("eastmoney", ["A"], frame("eastmoney"))
    reg = registry(primary, backup, clock=FrozenClock(datetime(2024, 1, 3, tzinfo=UTC)))

    result = reg.fetch_daily("A", "600519", D1, D2)
    assert result.source == "eastmoney", "主源被限流要立刻换备源"
    assert result.degraded
    assert [a.source for a in result.attempts] == ["akshare", "eastmoney"]
    by_name = {h.name: h for h in reg.health()}
    assert not by_name["akshare"].ok and by_name["akshare"].consecutive_failures == 1
    assert by_name["eastmoney"].ok and by_name["eastmoney"].last_ok_at == date(2024, 1, 3)


def test_field_drift_switches_source_without_wasting_retries():
    """改版重试不会变好：一次就定性，立刻换源。"""
    primary = FakeAdapter("akshare", ["A"], FieldDrift("akshare", "少列"))
    backup = FakeAdapter("eastmoney", ["A"], frame("eastmoney"))
    reg = registry(primary, backup)
    result = reg.fetch_daily("A", "600519", D1, D2)
    assert primary.calls == 1
    assert result.source == "eastmoney"


def test_transient_error_is_retried_with_backoff_before_switching():
    sleeps: list[float] = []
    primary = FakeAdapter(
        "akshare",
        ["A"],
        TransientError("akshare", "timeout"),
        TransientError("akshare", "timeout"),
        RateLimited("akshare", "429"),
    )
    backup = FakeAdapter("eastmoney", ["A"], frame("eastmoney"))
    reg = AdapterRegistry(
        adapters={primary.name: primary, backup.name: backup},
        market_sources={"A": ("akshare", "eastmoney")},
        attempts_per_source=3,
        sleep=sleeps.append,
    )
    result = reg.fetch_daily("A", "600519", D1, D2)
    assert primary.calls == 3
    assert sleeps == [1.0, 4.0], "两次重试之间按 1、4 秒退避"
    assert result.source == "eastmoney"


def test_every_source_failing_raises_with_both_reasons():
    reg = registry(
        FakeAdapter("akshare", ["A"], RateLimited("akshare", "429")),
        FakeAdapter("eastmoney", ["A"], FieldDrift("eastmoney", "少列")),
    )
    with pytest.raises(DatasourceError, match="所有源均失败") as excinfo:
        reg.fetch_daily("A", "600519", D1, D2)
    assert "akshare" in str(excinfo.value) and "eastmoney" in str(excinfo.value)
    assert all(not h.ok for h in reg.health())


class InterruptingAdapter:
    """用户按了 Ctrl-C：这不是「这个源坏了，换下一个」。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.markets = frozenset({"A"})
        self.calls = 0

    def daily_bars(self, market, code, start, end):
        self.calls += 1
        raise KeyboardInterrupt


def test_interrupt_is_not_a_source_failure():
    """兜底必须是 Exception 而不是 BaseException：兜住 Ctrl-C 会连着犯三个错——

    中断被当成源故障记进健康表，整条链每个源都要再试一遍才退出，
    而那个源下一次被正常选中去背这次的账。
    """
    primary = InterruptingAdapter("akshare")
    backup = FakeAdapter("eastmoney", ["A"], frame("eastmoney"))
    reg = registry(primary, backup)
    with pytest.raises(KeyboardInterrupt):
        reg.fetch_daily("A", "600519", D1, D2)
    assert backup.calls == 0, "用户要停，不该接着打备源"
    # 三态的 `ok`：None 是「从没被路由过」。中断必须停在这里——记成 False 就是
    # 把一次 Ctrl-C 存成源故障，而下一个人看到的健康页会说这个源有问题
    assert all(h.ok is None and h.consecutive_failures == 0 for h in reg.health())


def test_all_sources_empty_is_reported_as_empty_not_failure():
    """停牌：两个源都回答「没有数据」，这不是采集失败。"""
    reg = registry(
        FakeAdapter("akshare", ["A"], EmptyResponse("akshare", "无")),
        FakeAdapter("eastmoney", ["A"], EmptyResponse("eastmoney", "无")),
    )
    result = reg.fetch_daily("A", "600519", D1, D2)
    assert result.empty_everywhere and result.frame.is_empty()
    assert result.frame.columns == BAR_COLUMNS


def test_source_that_doesnt_cover_the_market_is_caught_at_construction():
    """把源派给它不支持的市场是配置错误，不该等到采集时「跳过但不留痕」：
    那样健康检查只会看到「这个源从没被调用过」，而采集早就少了一个源。"""
    with pytest.raises(DatasourceError, match="不支持它"):
        AdapterRegistry(
            adapters={"yfinance": FakeAdapter("yfinance", ["US"], frame("yfinance"))},
            market_sources={"A": ("yfinance",)},
        )


def test_market_without_sources_or_unknown_market_fails_clearly():
    reg = registry(FakeAdapter("akshare", ["A"], frame("akshare")), markets=("A",))
    with pytest.raises(DatasourceError, match="JP"):
        reg.fetch_daily("JP", "AAPL", D1, D2)


def test_configured_but_unregistered_source_is_caught_at_construction():
    with pytest.raises(DatasourceError, match="未注册"):
        AdapterRegistry(
            adapters={"akshare": FakeAdapter("akshare", ["A"], frame("akshare"))},
            market_sources={"A": ("akshare", "sina")},
        )


def test_bucket_is_consulted_for_every_call():
    clock = {"now": 0.0}
    waits: list[float] = []

    def advance_sleep(seconds):
        waits.append(seconds)
        clock["now"] += seconds

    bucket = TokenBucket(rate=1.0, capacity=1.0, clock=lambda: clock["now"], sleep=advance_sleep)
    primary = FakeAdapter("akshare", ["A"], frame("akshare"), frame("akshare"))
    reg = AdapterRegistry(
        adapters={primary.name: primary},
        market_sources={"A": ("akshare",)},
        bucket=bucket,
        sleep=advance_sleep,
    )
    reg.fetch_daily("A", "600519", D1, D2)
    reg.fetch_daily("A", "600519", D1, D2)
    assert waits, "第二次请求必须被限速器按住"


def test_unexpected_adapter_error_does_not_escape_the_router():
    reg = registry(FakeAdapter("akshare", ["A"], ZeroDivisionError("boom")))
    with pytest.raises(DatasourceError, match="ZeroDivisionError"):
        reg.fetch_daily("A", "600519", D1, D2)


def test_from_settings_uses_configured_source_order(settings):
    stubs = {
        name: FakeAdapter(name, ["A", "US", "HK"], frame(name))
        for name in ("akshare", "eastmoney", "yfinance")
    }
    reg = AdapterRegistry.from_settings(settings, adapters=stubs, bucket=None)
    assert reg.sources_for("A") == ("akshare", "eastmoney")
    assert reg.sources_for("US") == ("yfinance",)
    assert {h.name for h in reg.health()} == {"akshare", "eastmoney", "yfinance"}


def test_failures_survive_into_the_next_process(db):
    """健康记录落库：`sams health` 是另一个进程，不该只看自己的内存。"""
    store = SourceHealthRepo(db)
    failing = registry(FakeAdapter("akshare", ["A"], RateLimited("akshare", "429")), store=store)
    with pytest.raises(DatasourceError):
        failing.fetch_daily("A", "600519", D1, D2)

    reopened = registry(FakeAdapter("akshare", ["A"], RateLimited("akshare", "429")), store=store)
    assert [(r.name, r.ok, r.consecutive_failures) for r in reopened.health()] == [
        ("akshare", False, 1)
    ]


def test_recovered_source_clears_the_stored_failure(db):
    store = SourceHealthRepo(db)
    broken = registry(FakeAdapter("akshare", ["A"], RateLimited("akshare", "429")), store=store)
    with pytest.raises(DatasourceError):
        broken.fetch_daily("A", "600519", D1, D2)

    fixed = registry(FakeAdapter("akshare", ["A"], frame("akshare")), store=store)
    fixed.fetch_daily("A", "600519", D1, D2)
    assert SourceHealthRepo(db).load()["akshare"].ok is True


def test_source_never_called_is_unknown_not_ok():
    """三态健康：没被路由过的源不能显示成 ok。

    港美股只有 yfinance 一个源，若「未知」被渲染成正常，单点故障就永远不出现在报告上。
    """
    reg = registry(FakeAdapter("akshare", ["A"], frame("akshare")))
    assert reg.health()[0].ok is None


def test_consecutive_failures_publish_exactly_one_system_alarm():
    bus = EventBus()
    seen: list[SystemAlarm] = []
    bus.subscribe(SystemAlarm, seen.append)
    reg = registry(FakeAdapter("akshare", ["A"], RateLimited("akshare", "429")), bus=bus)

    for _ in range(4):
        with pytest.raises(DatasourceError):
            reg.fetch_daily("A", "600519", D1, D2)

    # 只在跨过阈值那一次告警：之后每次都通知等于没有通知
    assert [a.component for a in seen] == ["datasource:akshare"]
    assert "连续 3 次失败" in seen[0].message


def test_success_stamp_comes_from_the_injected_clock():
    clock = FrozenClock(datetime(2024, 5, 6, tzinfo=UTC))
    reg = registry(FakeAdapter("akshare", ["A"], frame("akshare")), clock=clock)
    reg.fetch_daily("A", "600519", D1, D2)
    assert reg.health()[0].last_ok_at == date(2024, 5, 6)


def test_unattempted_source_stays_unknown_across_processes(db):
    """库里没有这个源的行 ≠ 库里记录为失败：读回来必须仍是未知。"""
    store = SourceHealthRepo(db)
    registry(FakeAdapter("akshare", ["A"], frame("akshare")), store=store)
    assert SourceHealthRepo(db).load() == {}, "没人调用过就不该有记录"

    reopened = registry(FakeAdapter("akshare", ["A"], frame("akshare")), store=store)
    assert reopened.health()[0].ok is None


# --- 公司行为链：按能力路由（D38） ------------------------------------------------


def actions_frame(source: str) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "market": ["A"],
            "code": ["601088"],
            "ex_date": [date(2024, 7, 8)],
            "kind": ["dividend"],
            "cash_per_share": [2.26],
            "ratio": [None],
            "price": [None],
            "src": [source],
            "note": ["10派22.60元(含税)"],
        }
    )


class ActionsAdapter:
    """只有公司行为能力的源；`full_history=False` 模拟按区间拉的接口。"""

    def __init__(
        self, name: str, markets=("A",), outcome=None, *, full_history=True, error=None
    ) -> None:
        self.name = name
        self.markets = frozenset(markets)
        self.outcome = actions_frame(name) if outcome is None else outcome
        self.corp_actions_full_history = full_history
        self.error = error
        self.calls = 0

    def corp_actions(self, market, code, start, end):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.outcome


def test_actions_chain_skips_sources_without_the_capability():
    """跳过要留名：静默跳过的症状是「这只票从没分过红」，而真相是链上没人能给。"""
    bars_only = FakeAdapter("tencent", ["A"], frame("tencent"))
    reg = registry(bars_only, ActionsAdapter("akshare"))
    result = reg.fetch_actions("A", "601088", D1, D2)
    assert (result.source, result.frame.height) == ("akshare", 1)
    assert result.skipped == (("tencent", "不提供公司行为"),)
    assert bars_only.calls == 0


def test_daily_chain_skips_sources_without_bars():
    acts_only = ActionsAdapter("akshare_actions_only")
    reg = registry(acts_only, FakeAdapter("eastmoney", ["A"], frame("eastmoney")))
    result = reg.fetch_daily("A", "600519", D1, D2)
    assert result.source == "eastmoney"
    assert result.skipped == (("akshare_actions_only", "不提供日线"),)
    assert acts_only.calls == 0


def test_full_history_flag_follows_the_adapter():
    reg = registry(ActionsAdapter("akshare", full_history=True))
    assert reg.fetch_actions("A", "601088", D1, D2).full_history is True
    reg2 = registry(ActionsAdapter("sina", full_history=False))
    assert reg2.fetch_actions("A", "601088", D1, D2).full_history is False


def test_actions_empty_response_switches_source_without_burning_health():
    """公司行为的「无数据」不是成功：覆盖标记会把没查过写成查过（D36）。"""
    quiet = ActionsAdapter("yfinance", error=EmptyResponse("yfinance", "区间无数据"))
    reg = registry(quiet, ActionsAdapter("akshare"))
    result = reg.fetch_actions("A", "601088", D1, D2)
    assert result.source == "akshare"
    assert result.degraded is True
    record = {r.name: r for r in reg.health()}["yfinance"]
    assert (record.ok, record.consecutive_failures) == (None, 0)


def test_actions_failure_names_both_the_skipped_and_the_failed():
    """运维只读一行就要分清「链上没有这个能力」与「有能力但这次挂了」。"""
    broken = ActionsAdapter("akshare", error=FieldDrift("akshare", "列名改版"))
    reg = registry(FakeAdapter("tencent", ["A"], frame("tencent")), broken)
    with pytest.raises(DatasourceError, match="不提供公司行为"):
        reg.fetch_actions("A", "601088", D1, D2)
    with pytest.raises(DatasourceError, match="列名改版"):
        reg.fetch_actions("A", "601088", D1, D2)


# --- 「这个源说没有」不等于「这只票没有」：空答复不许终结这条链 ----------------------


def test_an_empty_answer_does_not_end_the_chain_when_another_source_has_records():
    """实测 002450：东财回「返回数据为空」，同花顺有 8 条除权。

    空帧在旧语义里是一次成功取数，于是主源一句「我这儿没有」就把链走完了：服务层写下
    「全历史，确认至今天」，库里 0 行——覆盖标记与库互相打脸，跨那天的区间退回裸价（D36/D40）。
    """
    quiet = ActionsAdapter("akshare", outcome=empty_actions())
    rich = ActionsAdapter("akshare_ths")
    reg = registry(quiet, rich)
    result = reg.fetch_actions("A", "002450", D1, D2)
    assert result.source == "akshare_ths"
    assert result.frame.height == 1
    assert result.confirmed_empty_by == ()
    assert result.degraded is True


def test_the_whole_chain_saying_no_records_is_an_answer_we_can_record():
    """两个独立的源都说没有，「这只票从没分过红」才配写成覆盖标记。"""
    reg = registry(ActionsAdapter("akshare", outcome=empty_actions()),
                   ActionsAdapter("akshare_ths", outcome=empty_actions()))
    result = reg.fetch_actions("A", "600000", D1, D2)
    assert result.frame.is_empty()
    assert result.confirmed_empty_by == ("akshare", "akshare_ths")
    assert result.full_history is True
    assert result.source == "akshare"


def test_a_corroborated_claim_needs_every_source_to_have_answered():
    """一个说没有 + 一个挂了 ⇒ 谁都不能替挂掉的那个确认「没有」。拒绝写覆盖，继续换天再查。"""
    reg = registry(
        ActionsAdapter("akshare", outcome=empty_actions()),
        ActionsAdapter("akshare_ths", error=FieldDrift("akshare_ths", "列名改版")),
    )
    with pytest.raises(DatasourceError, match="列名改版"):
        reg.fetch_actions("A", "600000", D1, D2)


def test_a_window_scoped_source_cannot_corroborate_a_full_history_claim():
    """按区间拉的源说「这段没有」，与全历史源说的「历史里没有」是两句话（D36）。"""
    reg = registry(
        ActionsAdapter("akshare", outcome=empty_actions(), full_history=True),
        ActionsAdapter("sina", outcome=empty_actions(), full_history=False),
    )
    assert reg.fetch_actions("A", "600000", D1, D2).full_history is False
