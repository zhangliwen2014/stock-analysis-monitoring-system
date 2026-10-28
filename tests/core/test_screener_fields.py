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
