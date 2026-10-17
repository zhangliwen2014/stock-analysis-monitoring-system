"""yfinance 公司行为（`Ticker.get_actions(period="max")`）。

 pandas 帧是录制形状，不是断言目标：这里要钉住的是「除息日落在哪一天」与
 「哪一列能折出数」，而不是 Yahoo 的列名拼写。
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from sams.core.errors import FieldDrift
from sams.datasource.yfinance_us import YfinanceAdapter, expand_actions


def actions_frame(index, **columns) -> object:
    import pandas as pd

    return pd.DataFrame(columns, index=pd.DatetimeIndex(index, name="Date"))


def hk_midnight(*days: str):
    """港股的除息日在 Yahoo 索引里是**当地零点**：转成 UTC 会整体早一天。"""
    import pandas as pd

    return pd.to_datetime([f"{d} 00:00" for d in days]).tz_localize("Asia/Hong_Kong")


# --- 日期：交易所本地，不是 UTC ----------------------------------------------------


def test_ex_date_is_the_exchange_local_calendar_day():
    out = expand_actions(
        actions_frame(hk_midnight("2024-05-17"), **{"Dividends": [0.34]})
    )
    assert out["ex_date"].to_list() == [date(2024, 5, 17)]


def test_us_midnight_survives_the_same_way():
    import pandas as pd

    index = pd.to_datetime(["2024-03-15 00:00"]).tz_localize("America/New_York")
    out = expand_actions(actions_frame(index, **{"Dividends": [0.24]}))
    assert out["ex_date"].to_list() == [date(2024, 3, 15)]


# --- 列 → 标准列 ------------------------------------------------------------------


def test_dividend_and_split_on_the_same_day_are_two_rows():
    out = expand_actions(
        actions_frame(
            [date(2024, 6, 3)],
            **{"Dividends": [1.0], "Stock Splits": [2.0]},
        )
    )
    assert out.sort("kind").select(["kind", "cash_per_share", "ratio"]).to_dicts() == [
        {"kind": "dividend", "cash_per_share": 1.0, "ratio": None},
        {"kind": "split", "cash_per_share": None, "ratio": 2.0},
    ]


def test_a_split_of_one_is_not_an_event():
    """Yahoo 在没有拆股的行上填 1.0：把它当一次「1:1 拆股」会写出一条无意义的记录。"""
    out = expand_actions(
        actions_frame([date(2024, 6, 3)], **{"Dividends": [1.0], "Stock Splits": [1.0]})
    )
    assert out["kind"].to_list() == ["dividend"]


def test_zero_dividend_row_produces_nothing():
    assert expand_actions(actions_frame([date(2024, 6, 3)], **{"Dividends": [0.0]})).is_empty()


def test_reverse_split_is_a_shrinking_multiplier():
    out = expand_actions(actions_frame([date(2024, 6, 3)], **{"Stock Splits": [0.5]}))
    assert out["ratio"].to_list() == [0.5]


def test_capital_gains_distribution_counts_as_cash_out():
    out = expand_actions(
        actions_frame([date(2024, 6, 3)], **{"Dividends": [0.1], "Capital Gains": [0.02]})
    )
    assert out["cash_per_share"].to_list() == [0.12]
    assert "资本利得" in out["note"][0]


def test_no_action_column_at_all_is_drift():
    """认不出列就是改版：返回空等于宣布「这只票从没分过红」，而它年年分。"""
    with pytest.raises(FieldDrift, match="Dividends"):
        expand_actions(actions_frame([date(2024, 6, 3)], **{"Earnings": [1.0]}))


# --- 适配器：窗口、空答案、能力声明 ------------------------------------------------


def adapter(*days, **columns) -> YfinanceAdapter:
    frame = actions_frame(list(days), **columns)
    return YfinanceAdapter(actions_fetch=lambda symbol: frame)


def test_corp_actions_returns_the_standard_columns_for_the_window():
    out = adapter(date(2023, 1, 1), date(2024, 6, 3), **{"Dividends": [0.0, 1.0]}).corp_actions(
        "US", "AAPL", date(2024, 1, 1), date(2024, 12, 31)
    )
    assert out.columns == [
        "market",
        "code",
        "ex_date",
        "kind",
        "cash_per_share",
        "ratio",
        "price",
        "src",
        "note",
    ]
    assert out["ex_date"].to_list() == [date(2024, 6, 3)]
    assert out["src"].to_list() == ["yfinance"]


def test_a_symbol_that_never_paid_out_answers_empty_not_failed():
    """「从没分过红」是事实而不是故障：空帧才让覆盖标记写得下去（D36）。"""
    out = adapter(**{"Dividends": []}).corp_actions(
        "US", "AAPL", date(2024, 1, 1), date(2024, 2, 1)
    )
    assert out.is_empty()
    assert out.columns[0] == "market"


def test_window_without_events_is_also_an_empty_answer():
    out = adapter(date(2020, 1, 1), **{"Dividends": [1.0]}).corp_actions(
        "US", "AAPL", date(2024, 1, 1), date(2024, 2, 1)
    )
    assert out.is_empty()


def test_adapter_declares_both_capabilities():
    reg = YfinanceAdapter()
    assert hasattr(reg, "daily_bars") and hasattr(reg, "corp_actions")
    assert reg.corp_actions_full_history is True


@pytest.mark.live
def test_live_apple_dividends_are_dated_inside_the_requested_window():
    """Yahoo 在本机经常 429（`RateLimited`），所以这条默认不跑。

    它是唯一能证明「除息日取交易所本地日」这条路对的方法：录制帧的索引是我们自己造的，
    它对错位不敏感。
    """
    out = YfinanceAdapter().corp_actions("US", "AAPL", date(2024, 1, 1), date(2024, 12, 31))
    assert out.height >= 4, "苹果按季派息，一年至少四笔"
    assert set(out["kind"].to_list()) == {"dividend"}
    assert all(date(2024, 1, 1) <= d <= date(2024, 12, 31) for d in out["ex_date"].to_list())
    assert out.filter(pl.col("cash_per_share") <= 0).is_empty()
