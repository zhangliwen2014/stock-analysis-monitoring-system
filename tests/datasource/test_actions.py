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


def test_same_ex_date_and_kind_keeps_the_last_row():
    frame = pl.DataFrame(
        {
            "ex_date": [date(2024, 7, 8), date(2024, 7, 8)],
            "kind": ["dividend", "dividend"],
            "cash_per_share": [2.26, 2.30],
        }
    )
    out = get(frame)
    assert out.height == 1 and out["cash_per_share"].to_list() == [2.30], "源回填改数取后一条"


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
