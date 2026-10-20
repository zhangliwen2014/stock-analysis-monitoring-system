"""yfinance 的估值快照：一次 `info` 调用给「今天的 PE/PB/市值」，一行都不多。

这一条链在港美股上的作用有限，代码里必须把有限写在脸上：

* **只有当日一行**。`Ticker.info` 是快照不是序列，所以这里不谎称能回填历史；
  按日估值序列在 HK 另有源（东财/百度，2020 年起）而不在本轮接入，US 侧则根本没有
  免费的按日 PE。**M2-D 要的「PE 历史分位」因此在本轮只能问 A 股**。
* **`dividendYield` 刻意不映射**（D58）。Yahoo 这个键的单位在版本之间漂移过
  （有的版本 0.52 表示 0.52%，有的表示 52%），而本机被限流、无法实测确认。
  一个未经核实的口径进库，后果是「股息率」那一列静默差一百倍——
  宁可这一列是 null（读取侧会明说「不知道」），也不要一个看着像数的错数。
* **日期属于交易日，不属于采集日**（与 `0012` 那条注释同一条理由）：
  周末跑一次同步，取到的是最后一个交易日的收盘估值，写成一行的日期必须是那一天。
  而 `regularMarketTime` 带交易所时区，取 UTC 日历日会让美东下午的快照落到前一天。
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from sams.core.errors import DatasourceError, EmptyResponse, FieldDrift
from sams.core.fundamentals import VALUATION_COLUMNS
from sams.datasource.sources import known
from sams.datasource.yfinance_us import YfinanceAdapter, snapshot_date

#: 2026-01-05 02:00 UTC = 2026-01-04 21:00 美东：**UTC 已是 5 号，当地还是 4 号**
NEW_YORK_PRE_MIDNIGHT_UTC = datetime(2026, 1, 5, 2, 0, tzinfo=ZoneInfo("UTC"))
#: 同一个时刻的秒级时间戳（Yahoo 两种形态都出现过）
EPOCH_SECONDS = NEW_YORK_PRE_MIDNIGHT_UTC.timestamp()


def info_payload(**over) -> dict:
    payload = {
        "regularMarketTime": NEW_YORK_PRE_MIDNIGHT_UTC,
        "exchangeTimezoneName": "America/New_York",
        "trailingPE": 31.4,
        "priceToBook": 45.2,
        "marketCap": 3.0e12,
        "dividendYield": 0.44,
    }
    payload.update(over)
    return payload


def adapter(info=None, **over) -> YfinanceAdapter:
    payload = info if info is not None else info_payload()

    def fetch(symbol: str) -> dict:
        fetch.symbols.append(symbol)
        return payload

    fetch.symbols = []
    return YfinanceAdapter(info_fetch=fetch, **over)


# ---------- 快照日期 ----------


def test_the_snapshot_is_dated_in_the_exchange_calendar_not_in_utc():
    assert snapshot_date(info_payload()) == date(2026, 1, 4)


def test_an_epoch_timestamp_and_a_tz_aware_datetime_are_the_same_trading_day():
    assert snapshot_date(info_payload(regularMarketTime=EPOCH_SECONDS)) == date(2026, 1, 4)


def test_a_naive_timestamp_is_refused_rather_than_guessed():
    """不带时区的时间戳没法确定「当地是几号」，而猜错的那天会让按日 PE 整段错位。"""
    with pytest.raises(EmptyResponse, match="时区"):
        snapshot_date(info_payload(regularMarketTime=datetime(2026, 1, 5, 2, 0)))


def test_a_snapshot_without_a_timestamp_is_an_empty_answer_not_a_zero_row_frame():
    with pytest.raises(EmptyResponse):
        snapshot_date(info_payload(regularMarketTime=None))


# ---------- valuation() ----------


def test_valuation_is_one_row_with_multiples_unchanged_and_the_key_canonical():
    out = adapter().valuation("US", "aapl", date(2026, 1, 1), date(2026, 12, 31))
    assert out.columns == list(VALUATION_COLUMNS)
    assert out.height == 1
    row = out.to_dicts()[0]
    assert (row["market"], row["code"]) == ("US", "AAPL")
    assert row["date"] == date(2026, 1, 4)
    assert row["pe_ttm"] == pytest.approx(31.4), "倍数不进 ÷100"
    assert row["pb"] == pytest.approx(45.2)
    assert row["market_cap"] == pytest.approx(3.0e12)


def test_the_dividend_yield_column_stays_unknown_because_the_unit_is_unverified():
    """info 里明明有 0.44，出口必须是 null：这一列的口径没核实过就不进库（D58）。"""
    out = adapter().valuation("US", "AAPL", date(2026, 1, 1), date(2026, 12, 31))
    assert out["dividend_yield"].to_list() == [None]


def test_a_negative_pe_survives_as_a_number():
    """亏损票的 trailingPE 是负数：擦成 null 就走进不了 D59 的第二支，
    而「PE 为负的票」正是市赚率要用 PB 配预估 ROE 去算的那一批。"""
    out = adapter(info=info_payload(trailingPE=-8.1)).valuation(
        "US", "AAPL", date(2026, 1, 1), date(2026, 12, 31)
    )
    assert out["pe_ttm"].to_list() == [pytest.approx(-8.1)]


def test_a_missing_multiple_is_null_not_zero():
    out = adapter(info=info_payload(trailingPE=None)).valuation(
        "US", "AAPL", date(2026, 1, 1), date(2026, 12, 31)
    )
    assert out["pe_ttm"].to_list() == [None]


def test_the_snapshot_window_filters_the_one_row_it_returns():
    """窗口按**交易日**切：`valuation_daily` 的粒度是日（D60），
    给一个不含 1 月 4 日的窗口就不该写这一行。"""
    out = adapter().valuation("US", "AAPL", date(2026, 2, 1), date(2026, 2, 28))
    assert out.height == 0


def test_the_key_leaves_canonical_even_when_the_input_is_short():
    """`700` 出去必须是 `00700`：估值与日线分两种键形时，PR 配不上对（D50）。"""
    out = adapter().valuation("HK", "700", date(2026, 1, 1), date(2026, 12, 31))
    assert out["market"].to_list() == ["HK"]
    assert out["code"].to_list() == ["00700"]


def test_an_upstream_failure_is_classified_not_leaked():
    def fetch(symbol):
        raise ConnectionError("429 Too Many Requests")

    with pytest.raises(DatasourceError) as exc:
        YfinanceAdapter(info_fetch=fetch).valuation(
            "US", "AAPL", date(2026, 1, 1), date(2026, 12, 31)
        )
    assert type(exc.value).__name__ == "RateLimited"


def test_an_unsupported_market_is_refused_before_the_network():
    calls: list[str] = []

    def fetch(symbol: str) -> dict:
        calls.append(symbol)
        return info_payload()

    with pytest.raises(ValueError, match="SG"):
        YfinanceAdapter(info_fetch=fetch).valuation(
            "SG", "AAPL", date(2026, 1, 1), date(2026, 12, 31)
        )
    assert calls == [], "市场不支持应该在调用网络之前就说出来"


def test_yfinance_now_offers_valuation_and_still_no_financials():
    """能力由方法是否存在推出来（D38）。这里没有 `financials` 是**有意的**：
    Yahoo 的免费接口给不出「这一期是什么时候公开的」，而 D61 的 PIT 闸门只认披露日——
    接进来只会写出一库永远读不到的行。`fx_rate` 是第四种能力，挂在同一个类上：
    它与日线是同一个源的两种能力，拆成两个 kind 会让「Yahoo 挂了」变成两条告警。"""
    assert known()["yfinance"].capabilities == (
        "daily_bars",
        "corp_actions",
        "valuation",
        "fx_rate",
    )


def test_a_payload_that_is_not_a_mapping_is_field_drift():
    """换回来的不是字典（接口改版/被代理页拦下）要说成改版，而不是 TypeError 一路冒到链上。"""
    with pytest.raises(FieldDrift, match="AAPL"):
        YfinanceAdapter(info_fetch=lambda symbol: []).valuation(
            "US", "AAPL", date(2026, 1, 1), date(2026, 12, 31)
        )
