"""新浪 A 股备源：「只给最近 N 根」这件事必须被写成代码，而不是注释。

这个源最危险的两种失败都是静默的：请求根数算少了 → 过滤后 0 根（看起来像「这段没数据」），
区间超出覆盖范围 → 接口只给最近 1023 根（看起来像一段完整的近历史）。
两条都有用例钉住。
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest
from support import FrozenClock, StubAdapter, sample_bars

from sams.core.errors import DatasourceError, EmptyResponse, FieldDrift, RateLimited, TransientError
from sams.datasource.minute import MINUTE_BARS_COLUMNS
from sams.datasource.registry import AdapterRegistry
from sams.datasource.sina import MAX_BARS, SinaAdapter, parse_sina_daily, parse_sina_minute

TODAY = date(2026, 10, 17)
#: 右端固定：接口给的是「最近 N 根到今天」，不冻住时钟就没法断言根数
CLOCK = FrozenClock(datetime(2026, 10, 17, 4, 0, tzinfo=UTC))

def bar(day: str, **fields) -> dict:
    row = {
        "day": day,
        "open": "1.0",
        "high": "1.5",
        "low": "0.9",
        "close": "1.2",
        "volume": "2400000",
    }
    row.update(fields)
    return row


RECORDED = [bar("2026-10-15"), bar("2026-10-16", close="1.4", volume="2100000")]

#: 真实响应长这样：一扇到今天为止的窗，既有请求区间内的日子，也有区间外的（右端是今天）
WIDE = [bar("2026-09-01"), bar("2026-09-02"), *RECORDED, bar("2026-10-17")]


def parse(payload=RECORDED, start=date(2026, 1, 1), end=TODAY, code="600519"):
    return parse_sina_daily(payload, code=code, start=start, end=end)


def test_recorded_payload_maps_day_to_date_and_keeps_volume_in_shares():
    out = parse()
    assert out["date"].to_list() == [date(2026, 10, 15), date(2026, 10, 16)]
    # 新浪的量已经是「股」：再乘 100 就把它写成 2.4 亿股/天
    assert out["volume"].to_list() == [2_400_000.0, 2_100_000.0]
    assert out["close"].to_list() == [1.2, 1.4]
    assert out["amount"].to_list() == [None, None]
    assert out["source"].unique().to_list() == ["sina"]


def test_json_text_response_is_accepted():
    assert parse(json.dumps(RECORDED)).height == 2


def test_rows_outside_the_requested_range_are_dropped():
    # 接口只认 datalen，区间靠这里裁：不裁就会把区间外的日子一起入库，
    # 于是 `sync` 的幂等承诺变成「多出来的日子从哪来」
    out = parse(RECORDED, start=date(2026, 10, 16), end=TODAY)
    assert out["date"].to_list() == [date(2026, 10, 16)]


@pytest.mark.parametrize(
    "payload,match",
    [
        ({}, "不是数组"),
        ('{"result": "error"}', "不是数组"),
        (["x"], "非对象元素"),
        ([{"close": "1.0"}], "缺少必需列"),
    ],
)
def test_wrong_shapes_are_named(payload, match):
    with pytest.raises(DatasourceError, match=match):
        parse(payload)


def test_empty_array_is_no_data_not_a_failure():
    with pytest.raises(EmptyResponse):
        parse([])


class FakeGet:
    def __init__(self, payload):
        self.payload = payload
        self.calls: list[dict] = []

    def __call__(self, url, params, *, timeout=15):
        self.calls.append(params)
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def adapter_with(payload):
    get = FakeGet(payload)
    return SinaAdapter(get=get, clock=CLOCK), get


def test_datalen_spans_start_to_today_not_start_to_end():
    """本模块存在的那条硬规则：数据右端是今天，按区间长度算根数就会少要。

    少要的后果是过滤后 0 根，而 0 根在 sync 的日志里和「这段确实没交易日」一模一样。
    """
    adapter, get = adapter_with(WIDE)
    frame = adapter.daily_bars("A", "600519", date(2026, 9, 1), date(2026, 9, 10))
    asked = int(get.calls[0]["datalen"])
    # 9/1 → 10/17 是 46 个日历天，5/7 折算约 33 根 + 缓冲；按区间长度只会要 11 根
    assert asked >= 33, f"只问了 {asked} 根，取不到 9 月的数据"
    assert frame["date"].to_list() == [date(2026, 9, 1), date(2026, 9, 2)]


def test_exactly_one_trading_day_range_still_asks_for_a_buffered_window():
    adapter, get = adapter_with(WIDE)
    frame = adapter.daily_bars("A", "600519", TODAY, TODAY)
    assert int(get.calls[0]["datalen"]) > 1
    assert frame["date"].to_list() == [TODAY]


def test_range_beyond_the_coverage_is_refused_instead_of_truncated():
    adapter, get = adapter_with(RECORDED)
    start = date(2020, 1, 1)
    with pytest.raises(DatasourceError, match="超过本源单次上限"):
        adapter.daily_bars("A", "600519", start, TODAY)
    assert get.calls == [], "拒绝必须发生在请求之前"


def test_in_range_empty_is_reported_as_no_data_so_the_chain_moves_on():
    """请求够了、区间内一根没有（那几天停市）：交回空帧会让路由以为成功。"""
    adapter, _ = adapter_with(RECORDED)
    with pytest.raises(EmptyResponse):
        adapter.daily_bars("A", "600519", date(2026, 10, 1), date(2026, 10, 5))


def test_the_cap_message_names_the_alternative():
    adapter, _ = adapter_with(RECORDED)
    with pytest.raises(DatasourceError, match="更早的历史请走其它源"):
        adapter.daily_bars("A", "600519", date(2022, 1, 1), TODAY)


def test_max_bars_is_a_business_day_window_not_a_day_count():
    # 1023 根日线 ≈ 4 年：写错成 1023 个日历天会让上面的判断偏一年
    assert MAX_BARS == 1023


def test_transport_error_is_classified():
    adapter, _ = adapter_with(ConnectionError("reset by peer"))
    with pytest.raises(TransientError):
        adapter.daily_bars("A", "600519", date(2026, 10, 1), TODAY)


# --- 指数日线（D148）：同一个端点，symbol 用源形态指数码原样 --------------------------


def test_sina_index_daily_a():
    adapter, get = adapter_with(WIDE)
    out = adapter.index_daily("A", "sh000001", date(2026, 9, 1), date(2026, 9, 10))
    # 不过 a_share_symbol：sh000001 已是新浪要的形态，再折一次会把指数问成个股或直接炸
    assert get.calls[0]["symbol"] == "sh000001"
    assert out["code"].unique().to_list() == ["sh000001"]
    assert out["date"].to_list() == [date(2026, 9, 1), date(2026, 9, 2)]


def test_sina_index_daily_hk_is_empty_answer():
    """新浪只有 A 股：别的市场是一句答复，降级链据此继续问下一个源。"""
    adapter, get = adapter_with(WIDE)
    with pytest.raises(EmptyResponse):
        adapter.index_daily("HK", "hkHSI", TODAY, TODAY)
    assert get.calls == [], "拒绝必须发生在请求之前"


def test_http_429_is_rate_limited_not_a_generic_failure(monkeypatch):
    """限流要退避重试同一个源，而不是立刻换源——那等于把备源也一起打爆。"""
    import sams.datasource.sina as module

    class Response429:
        status_code = 429

    monkeypatch.setattr("requests.get", lambda *a, **k: Response429())
    with pytest.raises(RateLimited):
        module.http_get("https://x", {}, timeout=1)


@pytest.mark.parametrize(
    "args,match",
    [
        (("UK", "600519", TODAY, TODAY), "不支持市场"),
        (("A", "600519", TODAY, date(2026, 1, 1)), "区间反向"),
    ],
)
def test_programming_errors_fail_fast(args, match):
    adapter, get = adapter_with(RECORDED)
    with pytest.raises(ValueError, match=match):
        adapter.daily_bars(*args)
    assert get.calls == []


def test_sina_says_no_data_and_the_registry_asks_the_next_source():
    """新浪排第三/四级时，它的 EmptyResponse 不该终结整条链。"""

    empty = SinaAdapter(get=FakeGet([]), clock=CLOCK)
    registry = AdapterRegistry(
        adapters={
            "sina": empty,
            "tencent": StubAdapter("tencent", ["A"], sample_bars(source="tencent")),
        },
        market_sources={"A": ("sina", "tencent")},
        attempts_per_source=1,
        sleep=lambda _s: None,
    )
    result = registry.fetch_daily("A", "600519", date(2026, 10, 1), TODAY)
    assert result.source == "tencent"
    assert not result.empty_everywhere


@pytest.mark.live
def test_live_sina_returns_the_recent_window():
    """要证明的是「新浪给得出最近的日线」，不是「此刻正在交易」。

    原来请求 `today~today`：盘前、周末、假期跑都会拿 0 根，而那 0 根是真话（那天的行情
    还不存在），失败的是用例自己的假设——一个每次停机都要重新解释的失败，等于没有断言。
    """
    today = date.today()
    since = today - timedelta(days=14)
    frame = SinaAdapter().daily_bars("A", "600519", since, today)
    assert frame.height >= 1, "最近两周里总该有交易日"
    days = frame["date"].to_list()
    assert min(days) >= since and max(days) <= today, "窗口外的根数不该混进来"
    assert min(frame["volume"].to_list()) > 0, "真实成交的那几根不该带 0 量"


# --- 分钟K（Task 5）：`day` 带秒，拆成 date + clock ------------------------------------

D28 = date(2026, 10, 28)
D27 = date(2026, 10, 27)
#: 分钟档不看时钟，但适配器一律要注入：冻在与样本同一天，断言就不依赖墙钟
CLOCK_D28 = FrozenClock(datetime(2026, 10, 28, 4, 0, tzinfo=UTC))


class RecordingGet:
    """记下整份 params：`scale=15` 有没有真的传下去，只有从参数上能证。"""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def __call__(self, url, params, *, timeout=15):
        self.calls.append((url, dict(params)))
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def minute_rows():
    return [
        {
            "day": "2026-10-27 15:00:00",
            "open": "1356.000",
            "high": "1357.000",
            "low": "1352.720",
            "close": "1355.900",
            "volume": "1194508",
            "amount": "1619962803.8680",
        },
        {
            "day": "2026-10-28 10:30:00",
            "open": "1354.980",
            "high": "1354.980",
            "low": "1333.000",
            "close": "1340.950",
            "volume": "1440368",
            "amount": "1936196723.9883",
        },
        {
            "day": "2026-10-28 11:05:00",
            "open": "1331.350",
            "high": "1331.430",
            "low": "1331.280",
            "close": "1331.410",
            "volume": "11000",
            "amount": "14644871.9688",
        },
    ]


def test_parse_sina_minute_splits_day_into_date_and_clock_and_trims_the_window():
    """`day` 带秒：拆成 `date` + `clock`，并按请求区间裁掉区间外那根。"""
    frame = parse_sina_minute(minute_rows(), code="600519", start=D28, end=D28)
    assert frame.columns == MINUTE_BARS_COLUMNS
    assert frame["date"].to_list() == [D28, D28]
    assert frame["clock"].to_list() == ["10:30", "11:05"]
    # 新浪的量本来就是股、额是元：与日线同一口径，不做换算
    assert frame["volume"].to_list() == [1440368.0, 11000.0]
    assert frame["amount"].to_list()[1] == pytest.approx(14644871.9688)


def test_minute_bars_asks_for_the_requested_scale_and_count():
    get = RecordingGet(minute_rows())
    SinaAdapter(get=get, clock=CLOCK_D28).minute_bars("A", "600519", D28, D28, scale=15, count=48)
    _url, params = get.calls[0]
    assert params["scale"] == "15"
    assert params["datalen"] == "48"
    assert params["symbol"] == "sh600519"


def test_minute_bars_refuses_other_markets_without_a_network_call():
    get = RecordingGet(minute_rows())
    with pytest.raises(EmptyResponse, match="只供"):
        SinaAdapter(get=get, clock=CLOCK_D28).minute_bars(
            "HK", "00700", D28, D28, scale=5, count=240
        )
    assert get.calls == []


def test_minute_bars_answers_no_data_when_the_window_is_older_than_the_rows():
    """请求够了、区间内一根没有：那是「这段没数据」，交 `EmptyResponse` 让链问下一个源。"""
    get = RecordingGet(minute_rows()[:1])
    with pytest.raises(EmptyResponse, match="0 根"):
        SinaAdapter(get=get, clock=CLOCK_D28).minute_bars(
            "A", "600519", D28, D28, scale=5, count=240
        )


def test_parse_sina_minute_rejects_a_row_without_a_time_part():
    with pytest.raises((FieldDrift, EmptyResponse)):
        parse_sina_minute(
            [{"day": "2026-10-28", "close": "1.0"}], code="600519", start=D28, end=D28
        )
