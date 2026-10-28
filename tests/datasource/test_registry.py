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
from sams.datasource.symbol_list import empty_symbol_list
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


# --- 降级链收敛成一份之后，两类取数的不同语义仍各归各（M2-C T-0） ----------------


def test_the_chain_stops_at_the_first_source_that_answers():
    """降级链不是广播：命中之后不该再敲后面的源——限速预算和延迟都在这句话里。

    链收敛成 `_walk` 之后这条最容易悄悄丢：把遍历写成「先把所有源问一遍再挑」，
    每个市场每次采集都会多出 N-1 次请求，而被跳过的源还会开始限流。
    """
    primary = FakeAdapter("primary", ["A"], frame("primary"))
    backup = FakeAdapter("backup", ["A"], frame("backup"))
    result = registry(primary, backup).fetch_daily("A", "600519", D1, D2)
    assert result.source == "primary"
    assert (primary.calls, backup.calls) == (1, 0)


def test_a_zero_row_bar_frame_is_a_hit_while_a_zero_row_action_frame_is_not():
    """同一个「0 行」在两类数据上不是同一句话（D43）。

    日线的 0 行是「这个区间确实没有交易」，是一次成功的取数，直接交付；
    公司行为的 0 行是「这只票在那张表里没有记录」，还不能写成覆盖确认，得继续问。
    链只上报发生了什么，性质由取数种类自己定——把两个 fetcher 合并成一个的那天，
    这两个断言会同时变红。
    """
    quiet_bars = FakeAdapter("sina", ["A"], frame("sina").clear())
    reg = registry(quiet_bars, FakeAdapter("akshare", ["A"], frame("akshare")))
    result = reg.fetch_daily("A", "600519", D1, D2)
    assert (result.source, result.frame.height, result.empty_everywhere) == ("sina", 0, False)
    assert (quiet_bars.calls, result.attempts[0].ok) == (1, True)

    asked_actions = ActionsAdapter("sina", outcome=empty_actions())
    actions = registry(asked_actions, ActionsAdapter("akshare")).fetch_actions(
        "A", "601088", D1, D2
    )
    assert (actions.source, actions.confirmed_empty_by) == ("akshare", ())
    assert asked_actions.calls == 1


def test_the_same_no_answer_is_ok_for_bars_and_not_ok_for_actions():
    """`EmptyResponse` 这个事件也一样：日线算答上了（停牌无数据），公司行为算没答上。

    后者若被当成「答上了但说没有」，覆盖标记就会把「从没查过」写成「查过且没有」，
    守卫从此对这只票放行裸价涨跌（D36 / D43 那条线上出过的错数）。
    """
    quiet = FakeAdapter("sina", ["A"], EmptyResponse("sina", "区间无数据"))
    bars = registry(quiet, FakeAdapter("akshare", ["A"], frame("akshare"))).fetch_daily(
        "A", "600519", D1, D2
    )
    assert bars.attempts[0].ok is True

    mute = ActionsAdapter("sina", error=EmptyResponse("sina", "区间无数据"))
    acts = registry(mute, ActionsAdapter("akshare")).fetch_actions("A", "601088", D1, D2)
    assert acts.attempts[0].ok is False


class AllFailAdapter:
    """每个能力都抛同一个异常：验「全链都失败」时终端错误是哪一种。"""

    markets = frozenset({"A"})

    def __init__(self, name: str, exc: Exception) -> None:
        self.name = name
        self.exc = exc

    def daily_bars(self, market, code, start, end):
        raise self.exc

    def corp_actions(self, market, code, start, end):
        raise self.exc

    def financials(self, market, code, start, end):
        raise self.exc


def _two_failing(*exc: Exception):
    return registry(
        AllFailAdapter("akshare", exc[0]),
        AllFailAdapter("eastmoney", exc[1]),
    )


def test_all_daily_sources_rate_limited_is_transient():
    """全链都是限流 ⇒ 「下轮再试」，不是「链坏了」（D104）。"""
    reg = _two_failing(RateLimited("akshare", "429"), RateLimited("eastmoney", "429"))
    with pytest.raises(TransientError, match="所有源均失败"):
        reg.fetch_daily("A", "600519", D1, D2)


@pytest.mark.parametrize(
    "excs",
    [
        (RateLimited("akshare", "429"), FieldDrift("eastmoney", "少列")),
        (FieldDrift("akshare", "少列"), RateLimited("eastmoney", "429")),
    ],
)
def test_one_structural_failure_makes_daily_terminal(excs):
    """掺进一个改版 ⇒ 出声告警，不带「重试就好」的意思。"""
    reg = _two_failing(*excs)
    with pytest.raises(DatasourceError) as caught:
        reg.fetch_daily("A", "600519", D1, D2)
    assert not isinstance(caught.value, TransientError)


def test_bare_network_errors_are_transient():
    """适配器漏归类的裸超时同样是「抖了一下」，不能因为被兜底包过一层就当成链坏了。"""
    reg = _two_failing(TimeoutError("timed out"), TimeoutError("timed out"))
    with pytest.raises(TransientError, match="所有源均失败"):
        reg.fetch_daily("A", "600519", D1, D2)


def test_actions_keep_retryability_in_the_terminal_error():
    reg = _two_failing(RateLimited("akshare", "429"), RateLimited("eastmoney", "429"))
    with pytest.raises(TransientError, match="取不到公司行为"):
        reg.fetch_actions("A", "600519", D1, D2)


def test_actions_with_a_no_answer_are_terminal():
    """公司行为的「答不上来」和基本面同理：一个源没把有无说清，就不能替它下结论。"""
    reg = _two_failing(
        RateLimited("akshare", "429"), EmptyResponse("eastmoney", "接口回空")
    )
    with pytest.raises(DatasourceError) as caught:
        reg.fetch_actions("A", "600519", D1, D2)
    assert not isinstance(caught.value, TransientError)


def test_frames_with_a_no_answer_are_terminal_even_if_the_rest_are_retryable():
    """「答不上来」不是「下轮再试」：它意味着这个源今天的答复不可读，必须出声。"""
    reg = _two_failing(
        RateLimited("akshare", "429"), EmptyResponse("eastmoney", "接口回空")
    )
    with pytest.raises(DatasourceError) as caught:
        reg.fetch_financials("A", "600519", D1, D2)
    assert not isinstance(caught.value, TransientError)


def test_frames_all_rate_limited_are_transient():
    reg = _two_failing(RateLimited("akshare", "429"), RateLimited("eastmoney", "429"))
    with pytest.raises(TransientError, match="取不到基本面"):
        reg.fetch_financials("A", "600519", D1, D2)


class _NoDaily:
    name = "nodaily"
    markets = frozenset({"A"})


def test_a_chain_with_no_daily_source_is_terminal():
    """一个能取日线的源都没有：这是配置缺口，重试不会让能力长出来。"""
    reg = registry(_NoDaily())
    with pytest.raises(DatasourceError, match="没有能取日线的源") as caught:
        reg.fetch_daily("A", "600519", D1, D2)
    assert not isinstance(caught.value, TransientError)


# --- SP1 两条新链：指数日线与代码表（D148） ------------------------------------------


class FakeIndexAdapter:
    """与 FakeAdapter 同一套按脚本应答，只是方法名换成 index_daily。"""

    def __init__(self, name: str, markets=("A",), *outcomes) -> None:
        self.name = name
        self.markets = frozenset(markets)
        self.outcomes = list(outcomes)
        self.calls = 0

    def index_daily(self, market, code, start, end):
        self.calls += 1
        outcome = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeSymbolListAdapter:
    """按脚本应答，方法是 symbol_list(market)：没有代码也没有窗口。"""

    def __init__(self, name: str, markets=("A",), *outcomes) -> None:
        self.name = name
        self.markets = frozenset(markets)
        self.outcomes = list(outcomes)
        self.calls = 0

    def symbol_list(self, market):
        self.calls += 1
        outcome = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def symbol_frame() -> pl.DataFrame:
    return pl.DataFrame({"code": ["000001", "600519"], "name": ["平安银行", "贵州茅台"]})


def test_fetch_index_daily_walks_chain():
    primary = FakeIndexAdapter("sina", ["A"], TransientError("sina", "timeout"))
    backup = FakeIndexAdapter("tencent", ["A"], frame("tencent"))
    reg = registry(primary, backup)
    result = reg.fetch_index_daily("A", "sh000001", D1, D2)
    assert result.source == "tencent"
    assert result.degraded
    assert [a.source for a in result.attempts] == ["sina", "tencent"]


def test_fetch_index_daily_all_empty_is_empty_not_failure():
    reg = registry(
        FakeIndexAdapter("sina", ["A"], EmptyResponse("sina", "无")),
        FakeIndexAdapter("tencent", ["A"], EmptyResponse("tencent", "无")),
    )
    result = reg.fetch_index_daily("A", "sh000001", D1, D2)
    assert result.empty_everywhere and result.frame.is_empty()
    assert result.frame.columns == BAR_COLUMNS


def test_fetch_index_daily_skips_sources_without_the_capability():
    bars_only = FakeAdapter("akshare", ["A"], frame("akshare"))
    with_index = FakeIndexAdapter("tencent", ["A"], frame("tencent"))
    reg = registry(bars_only, with_index)
    result = reg.fetch_index_daily("A", "sh000001", D1, D2)
    assert result.source == "tencent"
    assert result.skipped == (("akshare", "不提供指数日线"),)
    assert bars_only.calls == 0


def test_fetch_index_daily_chain_without_any_index_source_is_terminal():
    reg = registry(FakeAdapter("akshare", ["A"], frame("akshare")))
    with pytest.raises(DatasourceError, match="指数日线"):
        reg.fetch_index_daily("A", "sh000001", D1, D2)


def test_fetch_symbol_list_market_level():
    """代码表按市场路由：A 股链不该把港股源叫起来——它那张表根本不在同一个接口上。"""
    cn = FakeSymbolListAdapter("akshare", ["A"], symbol_frame())
    hk_rows = pl.DataFrame({"code": ["00700"], "name": ["腾讯控股"]})
    hk = FakeSymbolListAdapter("akshare_hk", ["HK"], hk_rows)
    reg = AdapterRegistry(
        adapters={"akshare": cn, "akshare_hk": hk},
        market_sources={"A": ("akshare",), "HK": ("akshare_hk",)},
        sleep=lambda _s: None,
    )
    result = reg.fetch_symbol_list("A")
    assert (result.source, result.degraded) == ("akshare", False)
    assert result.frame.to_dicts() == [
        {"code": "000001", "name": "平安银行"},
        {"code": "600519", "name": "贵州茅台"},
    ]
    assert (cn.calls, hk.calls) == (1, 0)


def test_fetch_symbol_list_transient_primary_falls_back_and_is_recorded():
    """主源瞬断不是这张表取不到：按降级链换备源，并把这次失败记进 attempts。"""
    primary = FakeSymbolListAdapter("akshare", ["A"], TransientError("akshare", "timeout"))
    backup = FakeSymbolListAdapter("akshare_ths", ["A"], symbol_frame())
    reg = registry(primary, backup)
    result = reg.fetch_symbol_list("A")
    assert result.source == "akshare_ths"
    assert result.degraded is True
    assert [(a.source, a.ok) for a in result.attempts] == [
        ("akshare", False),
        ("akshare_ths", True),
    ]


def test_fetch_symbol_list_without_capable_source_is_terminal():
    reg = registry(_NoDaily())
    with pytest.raises(DatasourceError, match="没有能取代码表的源"):
        reg.fetch_symbol_list("A")


def test_fetch_symbol_list_zero_rows_is_empty_everywhere():
    """0 行帧与 EmptyResponse 是同一句话：整条链都说没有才攒成 empty_everywhere。"""
    reg = registry(
        FakeSymbolListAdapter("akshare", ["A"], empty_symbol_list()),
        FakeSymbolListAdapter("akshare_ths", ["A"], EmptyResponse("akshare_ths", "表是空的")),
    )
    result = reg.fetch_symbol_list("A")
    assert result.empty_everywhere
    assert result.frame.is_empty()
    assert result.frame.columns == ["code", "name"]
    assert result.source == "akshare"
    assert [a.source for a in result.attempts] == ["akshare", "akshare_ths"]


# --- 分钟粒度两条链：把「要哪一档、要几根」传过降级链（Task 2） --------------------


class MinuteFake(FakeAdapter):
    """分时 + 分钟 K 双能力替身：分钟 K 把收到的 `scale`/`count` 留在 `extra` 上。"""

    def __init__(self, name, markets=("A",), *outcomes):
        super().__init__(name, markets, *outcomes)
        self.extra = None

    def minute_series(self, market, code, start, end):
        self.calls += 1
        outcome = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def minute_bars(self, market, code, start, end, *, scale, count):
        self.calls += 1
        self.extra = (scale, count)
        outcome = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_minute_bars_carries_the_scale_and_count_to_the_adapter():
    """`m15` 要真的变成 `scale=15`：适配器猜不到档，而猜错的档看起来完全正常。"""
    fake = MinuteFake("tencent", ("A",), frame("tencent"))
    result = registry(fake).fetch_minute_bars(
        "A", "600519", D1, D2, scale=15, count=240
    )
    assert fake.extra == (15, 240)
    assert result.source == "tencent"


def test_minute_series_asks_the_chain_for_one_day():
    """分时一次只答一天：`start == end == day`，链上不许把它当区间展开。"""
    fake = MinuteFake("tencent", ("A",), frame("tencent"))
    registry(fake).fetch_minute_series("A", "600519", D1)
    assert fake.calls == 1


def test_a_source_without_the_minute_method_is_skipped_with_the_right_words():
    """只有日线的源进 `skipped`，那句话是 `lacking`——不许静默跳过（D38）。"""
    daily = FakeAdapter("akshare", ("A",), frame("akshare"))
    minute = MinuteFake("tencent", ("A",), frame("tencent"))
    result = registry(daily, minute).fetch_minute_series("A", "600519", D1)
    assert result.source == "tencent"
    assert ("akshare", "不提供分时") in result.skipped


def test_empty_minute_result_carries_the_daily_shaped_frame():
    """每个源都答「没有」时 `frame` 是 `EMPTY_BARS`（**日线列集**）：读列名之前必须先看旗标。

    这一条是钉陷阱的：分钟服务若照日线那条路读 `frame["price"]`，撞上的不是「今天休市」
    而是 `ColumnNotFound`，而那次报错会被当成接口故障。
    """
    from sams.datasource.base import BAR_COLUMNS

    empty = MinuteFake("tencent", ("A",), EmptyResponse("tencent", "没有那一天"))
    result = registry(empty).fetch_minute_series("A", "600519", D1)
    assert result.empty_everywhere is True
    assert result.frame.columns == list(BAR_COLUMNS)
