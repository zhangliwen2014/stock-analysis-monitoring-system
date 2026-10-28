"""市赚率：两条分支、四种算不出，全部手算验证（D58 / D59）。

这些数字是刻意选小的：手算得出，读测试的人才能判断实现是不是写了它声称的那条公式。
"""

from __future__ import annotations

import pytest

from sams.analysis.fundamentals import (
    BASIS_PB_ESTIMATE,
    BASIS_PE,
    BASIS_REFUSED,
    pr,
    pr_now,
)


def test_the_scale_is_one_at_twenty_pe_and_twenty_percent_roe():
    """单位自检：`PR = PE ÷ ROE ÷ 100` 只有在 ROE 是小数口径时才落在「1 上下」。

    ROE 若按百分数存（20.0），这里会得到 0.01——屏幕上它长得像「极度低估」，
    而它只是把同一个数除了两遍 100。这条断言是 D58 存在的全部理由。
    """
    result = pr(pe_ttm=20.0, roe=0.20)
    assert result.value == pytest.approx(1.0)
    assert result.basis == BASIS_PE and result.estimated is False


def test_the_screening_threshold_itself_is_a_tested_number():
    """PR = 0.4 是选股命令要敲的那个门槛，边界值必须落在门槛里而不是外面。"""
    assert pr(pe_ttm=8.0, roe=0.20).value == pytest.approx(0.40)
    assert pr(pe_ttm=8.04, roe=0.20).value == pytest.approx(0.402, rel=1e-9)


@pytest.mark.parametrize(
    ("pe_ttm", "roe", "expected"),
    [
        (10.0, 0.40, 0.25),  # 好公司又便宜：ROE 40%、十倍市盈率
        (30.0, 0.10, 3.00),  # 一般公司还很贵：付三块钱买一块钱利润
        (5.0, 0.50, 0.10),
        (20.0, 0.05, 4.00),  # 低 ROE 会把 PR 顶上去，哪怕 PE 看起来不高
    ],
)
def test_positive_pe_hand_cases(pe_ttm: float, roe: float, expected: float):
    assert pr(pe_ttm=pe_ttm, roe=roe).value == pytest.approx(expected)


def test_pr_rises_with_pe_and_falls_with_roe():
    """单调性是「同一只票两个数谁更低」这件事的前提，不是理所当然的事。"""
    pe_values = [5.0, 10.0, 20.0, 40.0]
    at_20pct = [pr(pe_ttm=pe, roe=0.20).value for pe in pe_values]
    assert at_20pct == sorted(at_20pct)

    roe_values = [0.05, 0.10, 0.20, 0.40]
    at_20_pe = [pr(pe_ttm=20.0, roe=roe).value for roe in roe_values]
    assert at_20_pe == sorted(at_20_pe, reverse=True)


def test_the_negative_pe_branch_uses_pb_over_roe_twice():
    """PE≤0 那一支是 `PE ≈ PB ÷ ROE` 代回原式：ROE 要被除两次（D59）。

    ROE 用预估时 `estimated` 必须为真——屏幕上那句「这个数里嵌着一个人给的数」
    全靠它，而缺了它这条 PR 与已披露口径的 PR 看起来完全一样。
    """
    result = pr(pe_ttm=-5.0, pb=2.0, roe_estimate=0.20)
    assert result.value == pytest.approx(0.5)
    assert result.basis == BASIS_PB_ESTIMATE and result.estimated is True
    assert result.roe_used == 0.20
    assert "预估" in result.explain()


def test_the_same_pb_and_roe_are_the_same_number_whether_called_estimated_or_not():
    """公式不区分预估与已披露——区分的是**说出来的那句话**。"""
    from_estimate = pr(pe_ttm=None, pb=2.0, roe_estimate=0.20)
    from_reported = pr(pe_ttm=None, pb=2.0, roe=0.20)
    assert from_estimate.value == pytest.approx(from_reported.value)
    assert from_reported.estimated is False
    assert "已披露" in from_reported.explain()


def test_a_losing_company_without_an_estimate_gets_no_pr_rather_than_a_flattering_one():
    """这是这条公式最贵的一处：ROE 为负时 `PB ÷ ROE ÷ ROE` 的两次负号相消，
    会给出一个**正的、看起来很低**的 PR——于是亏损股排进「PR ≤ 0.4」的第一名。
    """
    result = pr(pe_ttm=-5.0, pb=2.0, roe=-0.10)
    assert result.value is None
    assert result.basis == BASIS_REFUSED
    assert "--roe-estimate" in result.reason


@pytest.mark.parametrize("pe_ttm", [-5.0, 0.0, None])
def test_missing_pe_says_so_instead_of_borrowing_the_negative_branch_message(pe_ttm):
    """PE 缺失该换源，PE 为负该给预估——两句下一步不同，所以不能共用一句文案。"""
    result = pr(pe_ttm=pe_ttm, pb=2.0)
    assert result.value is None
    if pe_ttm is None:
        assert "PE(TTM) 缺失" in result.reason
    else:
        assert "不为正" in result.reason


@pytest.mark.parametrize("pb", [None, 0.0, -1.0])
def test_the_pb_branch_still_needs_a_positive_pb(pb):
    """PB 为 0 时 `PB ÷ ROE ÷ ROE` 得 0，而 0 是「白送」——最便宜的 PR 由一个坏数据产生。"""
    result = pr(pe_ttm=-1.0, pb=pb, roe_estimate=0.20)
    assert result.value is None
    assert "PB" in result.reason


def test_zero_roe_refuses_instead_of_dividing_by_it():
    """不抛 ZeroDivisionError，也不返回 inf：两者都会被下游当成一个「数」。"""
    result = pr(pe_ttm=20.0, roe=0.0)
    assert result.value is None and result.basis == BASIS_REFUSED
    assert "ROE" in result.reason


def test_positive_pe_with_missing_roe_points_at_the_sync_command():
    result = pr(pe_ttm=18.5, roe=None)
    assert result.value is None
    assert "sams sync financials" in result.reason


def test_a_refusal_carries_its_reason_and_no_roe_was_used():
    """`ok` 是调用方唯一该问的问题；reason 非空是「拒绝也是一种答案」的下限。"""
    result = pr()
    assert (result.ok, result.value) == (False, None)
    assert result.reason and result.roe_used is None


def test_pr_now_is_pe_squared_over_pb():
    got = pr_now(18.68, 6.62)
    assert got.value == pytest.approx(18.68**2 / (100 * 6.62))
    assert got.reason is None


def test_pr_now_refuses_a_loss_maker_because_squaring_would_flip_the_sign():
    """PE 为负、平方后变正：不拦的话 `<= 0.5` 会把亏损票筛成「便宜」（D190）。"""
    got = pr_now(-8.0, 1.5)
    assert got.value is None and "亏损" in got.reason


def test_pr_now_refuses_negative_equity():
    got = pr_now(12.0, -0.3)
    assert got.value is None and "净资产为负" in got.reason


def test_pr_now_refuses_an_implied_roe_beyond_the_caliber_bound():
    got = pr_now(0.5, 0.9)  # 推出的 ROE = 1.8
    assert got.value is None and "上界" in got.reason


def test_pr_now_says_which_column_is_missing():
    assert "PE(动)" in pr_now(None, 1.0).reason
    assert "PB" in pr_now(10.0, None).reason

