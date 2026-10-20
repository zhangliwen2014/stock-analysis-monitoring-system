"""汇率采集的契约：一框 `(date, base, quote, rate, source)`，方向错了就抛。

`fx_pnl` 那一段能不能拆出来，全看这张表里有没有数，而「有没有数」里最贵的一种错法是
**方向反了**：源给的是 CNY→USD，我们要 USD→CNY，取倒数会得到一个「大约对」的数——
它对到足以让市值、成本、合计全都看着正常，又错到足以让每一笔跨币种盈亏都差几个百分点。
所以这里只按请求的方向收，反了就当没答上（`EmptyResponse`），不许悄悄换一个数。
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from sams.core.errors import EmptyResponse
from sams.datasource.fx import FX_COLUMNS, normalize_fx


def test_the_frame_carries_a_day_and_both_currencies() -> None:
    raw = pl.DataFrame(
        {"date": ["2024-10-25"], "base": ["USD"], "quote": ["CNY"], "rate": ["7.1"]}
    )
    out = normalize_fx(raw, base="USD", quote="CNY")
    assert out.columns == list(FX_COLUMNS)
    assert out["date"][0] == date(2024, 10, 25)


def test_a_direction_mismatch_is_refused() -> None:
    """源给了 CNY→USD 而我们要 USD→CNY：取倒数是「大约对」，方向写反是查不出的错。"""
    raw = pl.DataFrame(
        {"date": ["2024-10-25"], "base": ["CNY"], "quote": ["USD"], "rate": ["0.14"]}
    )
    with pytest.raises(EmptyResponse, match="方向"):
        normalize_fx(raw, base="USD", quote="CNY")


def test_an_empty_frame_is_empty_not_zero() -> None:
    with pytest.raises(EmptyResponse):
        normalize_fx(
            pl.DataFrame({"date": [], "base": [], "quote": [], "rate": []}),
            base="USD",
            quote="CNY",
        )


def test_a_datetime_stamp_still_names_the_day_it_belongs_to() -> None:
    """Yahoo 的索引是带时间（有时带时区）的：那一段后缀不是「哪一天」的一部分。

    带着时间戳落不进 `fx_rates` 的 DATE 列，而把它截错一位会把周五的数写成周四——
    锚在行情日的换算（D73）从此天天差一天。
    """
    raw = pl.DataFrame(
        {
            "date": ["2024-10-25 00:00:00", "2024-10-24T00:00:00+08:00"],
            "base": ["USD", "USD"],
            "quote": ["CNY", "CNY"],
            "rate": ["7.1", "7.05"],
        }
    )
    out = normalize_fx(raw, base="USD", quote="CNY")
    assert out["date"].to_list() == [date(2024, 10, 24), date(2024, 10, 25)]


def test_the_source_is_stamped_on_every_row() -> None:
    """数的出处跟着数一起出门（D54）：手工覆盖时要知道被覆盖的是哪一家的数。"""
    raw = pl.DataFrame(
        {"date": ["2024-10-25"], "base": ["USD"], "quote": ["CNY"], "rate": ["7.1"]}
    )
    out = normalize_fx(raw, base="USD", quote="CNY", source="yfinance")
    assert out["source"].to_list() == ["yfinance"]


def test_a_nan_rate_is_not_a_day_of_fx() -> None:
    """NaN 不是「汇率为 0」，是「这一行读不出数」：留着它，落库会当场拒收。"""
    raw = pl.DataFrame(
        {
            "date": ["2024-10-25", "2024-10-24"],
            "base": ["USD", "USD"],
            "quote": ["CNY", "CNY"],
            "rate": [7.1, None],
        }
    )
    out = normalize_fx(raw, base="USD", quote="CNY")
    assert out["date"].to_list() == [date(2024, 10, 25)]


@pytest.mark.live
def test_live_usd_cny_rate_arrives() -> None:
    """可达性是「一刻的事实」：Yahoo 429 或掐连接时 skip，不留永久红灯（同 U25）。

    skip 只兜「这台机器此刻没被理」（`RateLimited` / `TransientError`）；
    `EmptyResponse` 与 `FieldDrift` 照旧红——USD→CNY 这段有报价、列名是那几列，
    是这条用例唯一要验的事，降级链的形状本身由桩测守着。
    """
    from sams.core.errors import RateLimited, TransientError
    from sams.datasource.yfinance_us import YfinanceAdapter

    day = date(2026, 10, 16)
    try:
        out = YfinanceAdapter().fx_rate("USD", "CNY", day, day)
    except (RateLimited, TransientError) as exc:
        pytest.skip(f"Yahoo 此刻拒绝本机：{exc}")
    assert out["rate"][0] > 0
