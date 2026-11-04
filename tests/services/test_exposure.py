"""跨账户暴露与集中度（D232）。

口径：权重一律对 NAV（持仓市值 + 现金）；行业缺失归「未分类」而不是「其他」；
NAV 不为正时不给权重，并说一句为什么。
"""

from __future__ import annotations

import pytest

from sams.services.exposure import HoldingValue, exposure


def _h(market="A", code="600519", value=100.0, industry="白酒", name="贵州茅台"):
    return HoldingValue(
        market=market,
        code=code,
        name=name,
        currency={"A": "CNY", "HK": "HKD", "US": "USD"}[market],
        value=value,
        industry=industry,
    )


def test_weights_are_measured_against_nav_including_cash() -> None:
    """分母只算持仓会让权重和大于 100%，现金那一块从屏幕上消失。"""
    result = exposure([_h(value=750.0)], cash=250.0)
    assert result.total == pytest.approx(1_000.0)
    assert result.holdings[0].weight == pytest.approx(0.75)
    assert result.cash_weight == pytest.approx(0.25)


def test_top5_and_hhi_are_computed_over_holdings_not_cash() -> None:
    holdings = [_h(code=str(index), value=100.0) for index in range(7)]
    result = exposure(holdings, cash=300.0)
    assert result.top5_weight == pytest.approx(500.0 / 1_000.0)
    # 7 笔各 10%、现金 30%：HHI 只按持仓权重算，有效只数 = 1/HHI
    assert result.hhi == pytest.approx(7 * 0.01)
    # 有效只数只在持仓内部归一：现金不是仓，7 只等权就是 7
    assert result.effective_n == pytest.approx(7.0)


def test_a_missing_industry_is_unclassified_not_other() -> None:
    """「数据没有」与「确实在别的行业」不能合成一摞。"""
    result = exposure(
        [_h(value=60.0, industry=""), _h(code="000001", value=40.0, industry="银行")],
        cash=0.0,
    )
    labels = {row.label: row.weight for row in result.by_industry}
    assert labels["未分类"] == pytest.approx(0.6)
    assert "其他" not in labels
    assert result.unclassified_weight == pytest.approx(0.6)


def test_groups_by_market_and_currency_add_up_to_holdings() -> None:
    result = exposure(
        [
            _h(value=100.0),
            _h(market="HK", code="00700", value=300.0, industry="互联网"),
        ],
        cash=600.0,
    )
    assert {row.label: row.weight for row in result.by_market} == {
        "HK": pytest.approx(0.3),
        "A": pytest.approx(0.1),
    }
    assert {row.label: row.weight for row in result.by_currency} == {
        "HKD": pytest.approx(0.3),
        "CNY": pytest.approx(0.1),
    }


def test_a_non_positive_nav_yields_no_weights_and_says_why() -> None:
    """现金为负且大于持仓时，除法会给出一个负数权重；宁可不给。"""
    result = exposure([_h(value=100.0)], cash=-200.0)
    assert result.total == pytest.approx(-100.0)
    assert all(item.weight == 0.0 for item in result.holdings)
    assert result.cash_weight == 0.0
    assert result.notes and "权重" in result.notes[0]


def test_the_same_stock_in_two_accounts_is_one_exposure() -> None:
    """跨账户合并的是「同一笔赌注」，不是账户行数。"""
    result = exposure(
        [_h(value=100.0), _h(value=100.0), _h(code="000001", value=200.0)],
        cash=600.0,
    )
    assert [item.code for item in result.holdings] == ["600519", "000001"]
    assert result.holdings[0].weight == pytest.approx(0.2)
    assert result.top5_weight == pytest.approx(0.4)
    assert result.effective_n == pytest.approx(2.0)
