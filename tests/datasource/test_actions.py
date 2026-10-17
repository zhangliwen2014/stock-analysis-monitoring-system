"""`normalize_actions`：采集层到仓库层之间的那道形状关。

用例围绕一件事：什么样的响应应当**整批失败**，什么样的行应当丢掉并计数。
分错的后果不对称——多丢一行噪声无所谓，少乘一次除权会让区间收益偏掉而图上看不出来。
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from sams.core.errors import EmptyResponse, FieldDrift
from sams.datasource.actions import empty_actions, normalize_actions

BASE = {
    "ex_date": [date(2024, 7, 8)],
    "kind": ["dividend"],
    "cash_per_share": [2.26],
    "ratio": [None],
    "price": [None],
}


def raw(**overrides) -> pl.DataFrame:
    data = {**BASE, **overrides}
    return pl.DataFrame(data)


def get(frame: pl.DataFrame, **kwargs) -> pl.DataFrame:
    return normalize_actions(frame, market="A", code="601088", source="fake", **kwargs)


def test_standard_columns_in_order_and_ownership_filled():
    out = get(raw())
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
    assert out["market"].to_list() == ["A"]
    assert out["code"].to_list() == ["601088"]
    assert out["src"].to_list() == ["fake"]


def test_column_map_renames_source_side_names():
    frame = pl.DataFrame(
        {
            "除权日": [date(2024, 7, 8)],
            "类型": ["dividend"],
            "每股现金": [2.26],
        }
    )
    out = get(
        frame,
        column_map={
            "ex_date": "除权日",
            "kind": "类型",
            "cash_per_share": "每股现金",
        },
    )
    assert out["cash_per_share"].to_list() == [2.26]


def test_kind_is_case_insensitive_but_never_invented():
    """`Dividend` 与 `dividend` 是同一种行为；`spinoff` 是我们没实现过的第二种。"""
    assert get(raw(kind=["Dividend"]))["kind"].to_list() == ["dividend"]
    with pytest.raises(FieldDrift, match="未知的公司行为类型"):
        get(raw(kind=["spinoff"]))


def test_missing_required_column_is_drift_not_nulls():
    with pytest.raises(FieldDrift, match="缺少必需列"):
        get(pl.DataFrame({"ex_date": [date(2024, 7, 8)], "cash_per_share": [2.26]}))


def test_no_numeric_column_at_all_is_drift():
    """列整个不见了是改版；列在但值为空才是「这件事折不出数」（D39）。"""
    with pytest.raises(FieldDrift, match="三类数值列全缺"):
        get(pl.DataFrame({"ex_date": [date(2024, 7, 8)], "kind": ["dividend"]}))


def test_negative_number_is_drift():
    """负派息不存在。当成噪声丢掉它 = 少乘一次除权；按错位失败才有人去查。"""
    with pytest.raises(FieldDrift, match="数值不可能为负"):
        get(raw(cash_per_share=[-2.26]))


def test_rows_without_date_or_numbers_are_dropped_with_a_count(caplog):
    frame = pl.DataFrame(
        {
            "ex_date": [date(2024, 7, 8), None, date(2024, 9, 1)],
            "kind": ["dividend", "dividend", "dividend"],
            "cash_per_share": [2.26, 1.0, 0.0],
            "ratio": [None, None, None],
            "price": [None, None, None],
        }
    )
    # 0.0 是「给了一个数」——它推不出任何因子，但 shape 合法；仓库层的校验负责拒绝它
    out = get(frame)
    assert out["ex_date"].to_list() == [date(2024, 7, 8), date(2024, 9, 1)]
    assert "丢弃 A 601088 的公司行为 1 行" in caplog.text


def test_everything_dropped_is_empty_response():
    with pytest.raises(EmptyResponse):
        get(pl.DataFrame({"ex_date": [None], "kind": [None], "cash_per_share": [1.0]}))


def test_same_ex_date_and_kind_are_added_not_dropped():
    """同日同类两笔 ⇒ 现金相加。库的主键是 (market, code, ex_date, kind)，
    「留最后一条」等于第二笔从没发生过：年度分红与特别分红同日除权是真实事件，
    少一笔就是因子少乘一截，而价格序列照样单调、图上看不出异常。"""
    frame = pl.DataFrame(
        {
            "ex_date": [date(2024, 7, 8), date(2024, 7, 8)],
            "kind": ["dividend", "dividend"],
            "cash_per_share": [2.26, 2.30],
        }
    )
    out = get(frame)
    assert out.height == 1
    assert out["cash_per_share"].to_list() == [pytest.approx(4.56)]


def test_an_exact_repeat_row_is_not_double_counted():
    """两份一模一样的是同一条记录的重复披露，不是两笔事件：先按整行去重再相加。"""
    frame = pl.DataFrame(
        {
            "ex_date": [date(2024, 7, 8), date(2024, 7, 8)],
            "kind": ["dividend", "dividend"],
            "cash_per_share": [2.26, 2.26],
            "note": ["10派2.26元", "10派2.26元"],
        }
    )
    out = get(frame)
    assert out["cash_per_share"].to_list() == [pytest.approx(2.26)]


def test_two_rights_rows_keep_the_total_cash_they_cost():
    """配股价按新增股数加权合成：推导端算的是 `price × (ratio − 1)`，
    只有加权才保住两笔的应付总额，随便取一条会把它改小。"""
    frame = pl.DataFrame(
        {
            "ex_date": [date(2024, 7, 8), date(2024, 7, 8)],
            "kind": ["rights", "rights"],
            "cash_per_share": [None, None],
            "ratio": [1.2, 1.3],
            "price": [8.0, 4.0],
        }
    )
    out = get(frame)
    row = out.row(0, named=True)
    assert row["ratio"] == pytest.approx(1.5)
    # (8×0.2 + 4×0.3) / 0.5 = 5.6 ⇒ 5.6 × 0.5 = 2.8 = 1.6 + 1.2，与两笔原样相加一致
    assert row["price"] * (row["ratio"] - 1) == pytest.approx(8.0 * 0.2 + 4.0 * 0.3)


def test_nan_becomes_null_instead_of_poisoning_the_factor():
    frame = pl.DataFrame(
        {
            "ex_date": [date(2024, 7, 8)],
            "kind": ["dividend"],
            "cash_per_share": [float("nan")],
            "ratio": [1.5],
        }
    )
    out = get(frame)
    assert out["cash_per_share"].to_list() == [None]
    assert out["ratio"].to_list() == [1.5]


def test_dates_arriving_as_text_are_parsed():
    out = get(raw(ex_date=["2024-07-08"], kind=["dividend"]))
    assert out["ex_date"].to_list() == [date(2024, 7, 8)]
    with pytest.raises(EmptyResponse):
        get(raw(ex_date=["不是日期"]))


def test_empty_actions_is_a_successful_empty_frame_not_a_failure():
    frame = empty_actions()
    assert frame.is_empty()
    assert frame.columns == [
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
    assert frame.schema["ex_date"] == pl.Date


#: 三条数值列齐备但全空：这是「源报了一件折不出数的事」的形状，不是改版
_NO_NUMBERS = {"cash_per_share": [None], "ratio": [None], "price": [None]}


def test_an_unpriceable_row_is_kept_rather_than_dropped_as_noise():
    """D39：unknown 带着空数值是**有意**的记录，不是「三个数值全空」的那类噪声行。

    丢掉的后果不对称：多留一行噪声只是多一条拒答，少一行实物分派则让跨那天的涨跌
    悄悄偏掉几个点，而港美股没有涨跌停证据规则（D37）可兜。
    """
    frame = pl.DataFrame(
        {
            "ex_date": [date(2024, 7, 8)],
            "kind": ["unknown"],
            "note": ["以股代息，比例未披露"],
            **_NO_NUMBERS,
        }
    )
    out = get(frame)
    assert out["kind"].to_list() == ["unknown"]
    assert out["cash_per_share"].to_list() == [None]


def test_a_pricable_kind_with_no_numbers_at_all_is_still_noise():
    """反过来也成立：dividend 而三个数值全空，是没定案的预案，不该留下。"""
    with pytest.raises(EmptyResponse):
        get(pl.DataFrame({"ex_date": [date(2024, 7, 8)], "kind": ["dividend"], **_NO_NUMBERS}))
