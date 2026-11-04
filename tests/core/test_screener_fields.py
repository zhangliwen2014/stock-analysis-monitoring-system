"""选股词表：一个字段一行，坏条件在引擎之前就被拦下（D179）。

这一层是「屏幕上能选」与「服务端认」之间的唯一通道。每一枚用例钉一条会静默出错的
地方：字段名拼错被当成「没有票通过」、把百分数当小数、between 写反后自动交换、
两条不同口径的 PR 混进同一个篮子（D59）。
"""

from __future__ import annotations

import pytest

from sams.core.errors import ValidationError
from sams.core.screener_fields import FIELDS, Condition, Field, fields_for, validate


def test_unknown_field_lists_the_vocabulary():
    with pytest.raises(ValidationError, match="可选的是"):
        validate("A", [Condition("prr", "<=", 0.5)])


def test_market_without_the_field_is_refused_with_the_reason():
    # pr_now 只供 A 股（D188）：HK/US 那一行位置没法交叉验证，没注册
    with pytest.raises(ValidationError, match="只供"):
        validate("HK", [Condition("pr_now", "<=", 0.5)])


def test_empty_conditions_are_refused():
    # 空条件 = 全市场都通过，那是一条永远为真的筛法，重跑时看起来像「方案坏了」
    with pytest.raises(ValidationError, match="一条条件都没有"):
        validate("A", [])


def test_unknown_op_lists_the_operators():
    with pytest.raises(ValidationError, match="比较符"):
        validate("A", [Condition("price", "≈", 10.0)])


def test_between_takes_two_numbers_in_order():
    assert validate("A", [Condition("price", "between", (1.0, 10.0))])


def test_between_written_backwards_is_refused_not_swapped():
    # D58：静默替调用方改写请求，会让屏幕上那一行写的与跑的不是一回事
    with pytest.raises(ValidationError, match="写反了"):
        validate("A", [Condition("price", "between", (10.0, 1.0))])


def test_non_finite_values_are_refused():
    with pytest.raises(ValidationError, match="有限"):
        validate("A", [Condition("price", "<", float("nan"))])


def test_two_pr_baskets_cannot_share_one_basket():
    """D59/D177：两条市赚率公式算出的数不是同一个量，同篮排序会让「最便宜的」只对组内成立。"""
    registry = (
        *FIELDS,
        Field(
            "pr",
            "市赚率（年报口径）",
            "小数比值（0.5 = 50%）",
            "financials",
            "pr",
            frozenset({"A"}),
            basket="pr",
        ),
    )
    with pytest.raises(ValidationError, match="不同口径"):
        validate(
            "A",
            [Condition("pr_now", "<=", 0.5), Condition("pr", "<=", 0.5)],
            registry=registry,
        )


def test_registry_and_page_vocabulary_are_the_same_object():
    """词表只有一份：`fields_for` 是页面下拉与校验共用的那条路。"""
    names = {f.name for f in fields_for("A")}
    assert {"pr_now", "pe_dyn", "pb", "roe", "turnover_rate"} <= names
    assert "pr_now" not in {f.name for f in fields_for("HK")}


def test_hk_gets_the_verified_snapshot_columns_and_the_annual_financial_fields():
    """港股快照只有 57/58 等已与东财对过的格子（D188）；年报字段来自东财港股指标源。

    PEG 对港股开放：快照里的 PE(TTM) 与年报净利同比都在。股息率与 ROIC 这条源不给，
    仍只供 A（D228）。
    """
    names = {f.name for f in fields_for("HK")}
    assert {
        "price",
        "pct_chg",
        "amount",
        "amplitude",
        "pe_ttm",
        "pb",
        "market_cap",
        "float_cap",
        "peg",
        "roe",
        "revenue_yoy",
        "profit_yoy",
        "gross_margin",
        "net_margin",
        "roa",
        "debt_ratio",
        "current_ratio",
    } <= names
    assert names & {
        "pe_dyn",
        "pr_now",
        "turnover_rate",
        "volume_ratio",
        "dividend_yield",
        "roic",
    } == set()
    assert validate("HK", [Condition("peg", "<=", 1.0)])
    assert validate("HK", [Condition("roe", ">=", 0.15)])
    peg = {item.name: item for item in FIELDS}["peg"]
    assert peg.markets == frozenset({"A", "HK", "US"})
    assert "港股" in peg.note and "美股" in peg.note


def test_us_gets_the_snapshot_columns_and_the_annual_fields_its_source_provides():
    """美股快照 39/37/43/44/45 与新浪逐项对过；PB 没找到可核对的位置（D226）。

    东财美股 F10 给年报指标，但没有可核对的披露日（D228）：年报字段按报告期取。
    股息率与 ROIC 这条源不给，词表不为它们编数。
    """
    names = {f.name for f in fields_for("US")}
    assert {
        "price",
        "pct_chg",
        "amount",
        "amplitude",
        "pe_ttm",
        "market_cap",
        "float_cap",
        "peg",
        "roe",
        "revenue_yoy",
        "profit_yoy",
        "gross_margin",
        "net_margin",
        "roa",
        "debt_ratio",
        "current_ratio",
    } <= names
    assert names & {
        "pb",
        "pe_dyn",
        "pr_now",
        "turnover_rate",
        "volume_ratio",
        "dividend_yield",
        "roic",
    } == set()
    assert validate("US", [Condition("price", "<=", 10.0)])
    assert validate("US", [Condition("pe_ttm", "<=", 15.0)])
    assert validate("US", [Condition("roe", ">=", 0.15)])


def test_the_annual_quality_fields_are_shared_by_three_markets_but_two_stay_a_only():
    """D222 + D228：八个年报字段三个市场都有；比率都是小数（0.35 = 35%），流动比率才是倍。

    股息率与 ROIC 港股、美股两条源都不给，留在 A 股——词表说「能选」就等于引擎认，
    没有数的那一列必须灰着。
    """
    by_name = {item.name: item for item in FIELDS}
    shared = {
        "roe",
        "revenue_yoy",
        "profit_yoy",
        "gross_margin",
        "net_margin",
        "roa",
        "debt_ratio",
        "current_ratio",
    }
    assert shared <= set(by_name)
    assert all(
        by_name[name].markets == frozenset({"A", "HK", "US"}) for name in shared
    )
    assert all("小数" in by_name[name].unit for name in shared - {"current_ratio"})
    assert by_name["current_ratio"].unit == "倍"
    assert by_name["dividend_yield"].markets == frozenset({"A"})
    assert by_name["roic"].markets == frozenset({"A"})


def test_peg_declares_its_financial_input_and_a_missing_reason():
    """PEG 是推导列：它必须先拿到净利同比，否则引擎只会算出一列 None。"""
    peg = {item.name: item for item in FIELDS}["peg"]
    assert peg.source == "derived" and peg.column == "peg"
    assert peg.needs == ("profit_yoy",)
    assert "净利同比" in peg.missing_note


def test_roe_declares_the_inputs_of_its_fallback_formula():
    """ROE 仍是财务列（源给了就用源值），但引擎要先带进两个兜底输入（D228）。"""
    roe = {item.name: item for item in FIELDS}["roe"]
    assert roe.source == "financials"
    assert roe.needs == ("roa", "debt_ratio")
    assert "ROA" in roe.note and "资产负债率" in roe.note

def test_fields_declare_their_sensible_sort_direction():
    """D224：首批条件决定名单顺序；质量/成长降序，估值/负债升序，中性字段保持升序。"""
    by_name = {item.name: item for item in FIELDS}
    assert by_name["roe"].prefer == by_name["profit_yoy"].prefer == "high"
    assert by_name["pe_ttm"].prefer == by_name["debt_ratio"].prefer == "low"
    assert by_name["price"].prefer == ""


def test_describe_all_is_the_only_readable_spelling_of_a_condition_set():
    """方案列表要一句人话，不是 JSON；单位也必须在这句里（D58）。"""
    from sams.core.screener_fields import describe_all

    text = describe_all(
        [
            Condition("pr_now", "<=", 0.5),
            Condition("turnover_rate", ">=", 1.0),
            Condition("price", "between", (1.0, 5.0)),
        ]
    )
    assert text == (
        "现值市赚率 PR（小数比值（0.5 = 50%）） ≤ 0.5"
        " 且 换手率（百分数（0.31 = 0.31%）） ≥ 1"
        " 且 现价（元） 介于 1..5"
    )
