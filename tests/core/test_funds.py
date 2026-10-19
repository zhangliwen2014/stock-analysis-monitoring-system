"""币种推导与两段盈亏拆分（§5.4 硬规则 1/2）。纯函数，不碰库。"""

from __future__ import annotations

import pytest

from sams.core.config import MarketCfg
from sams.core.errors import ValidationError
from sams.core.funds import (
    FX_STALE_DAYS,
    PositionPnl,
    blended_cost,
    money_note,
    position_pnl,
    quote_currency,
    realized_pnl,
    weighted_rate,
)


def _cfg() -> dict[str, MarketCfg]:
    return {
        "A": MarketCfg(
            market="A", tz="Asia/Shanghai", currency="CNY",
            post_close_min=30, sources=("akshare",),
        ),
        "HK": MarketCfg(
            market="HK", tz="Asia/Hong_Kong", currency="HKD",
            post_close_min=30, sources=("akshare",),
        ),
        "US": MarketCfg(
            market="US", tz="America/New_York", currency="USD",
            post_close_min=30, sources=("yfinance",),
        ),
    }


@pytest.fixture
def markets() -> dict[str, MarketCfg]:
    return _cfg()


def test_a_share_is_cny_and_hk_is_hkd(markets) -> None:
    assert quote_currency("A", "601088", markets) == "CNY"
    assert quote_currency("HK", "00700", markets) == "HKD"
    assert quote_currency("US", "AAPL", markets) == "USD"


def test_dual_counter_and_b_share_override_the_market_currency(markets) -> None:
    """同一个市场里可以有两种币种（`core/codes.py:52`，D31）。

    忽略 override 的后果是把 USD/HKD 报价的 B 股按 CNY 记账，或把 HK 的人民币
    双柜台按 HKD 记账：同一只票的两个柜台会被读成两个不同成本的持仓，
    并永久显示一个不存在的「折价」。数值以 `currency_override` 为准——
    沪市 B 股（900*）USD、深市 B 股（200*）HKD、港股 8xxxx 段 CNY。
    """
    assert quote_currency("A", "900901", markets) == "USD"
    assert quote_currency("A", "200011", markets) == "HKD"
    assert quote_currency("HK", "80700", markets) == "CNY"
    assert quote_currency("HK", "00700", markets) == "HKD"


def test_unknown_market_is_a_refusal_not_a_default(markets) -> None:
    """给不出币种就不能记账：默默兜成 CNY 会让未配置市场的所有单都「成功」。"""
    with pytest.raises(ValidationError, match="SG"):
        quote_currency("SG", "D05", markets)


def test_a_second_buy_moves_the_average_cost_toward_the_new_price() -> None:
    # 100 股 @10，再 100 股 @12 → 均价 11；手续费计入成本（§5.4）
    assert blended_cost(100, 10.0, 100, 12.0, fee=0.0) == pytest.approx(11.0)
    assert blended_cost(100, 10.0, 100, 12.0, fee=20.0) == pytest.approx(11.1)


def test_blended_cost_on_an_empty_position_is_the_new_price() -> None:
    assert blended_cost(0, 0.0, 300, 7.5, fee=0.0) == pytest.approx(7.5)


def test_a_minus_fee_sale_realizes_pnl_against_the_average_cost() -> None:
    assert realized_pnl(qty=100, price=12.0, avg_cost=10.0, fee=15.0) == pytest.approx(185.0)


def test_selling_nothing_realizes_nothing() -> None:
    assert realized_pnl(qty=0, price=12.0, avg_cost=10.0, fee=0.0) == 0.0


def test_weighted_rate_of_nothing_is_none() -> None:
    """不是 0：把「没有汇率」读成 0 会把整段市值抹平。"""
    assert weighted_rate([]) is None


def test_weighted_rate_drops_the_stale_entry_instead_of_diluting_with_it() -> None:
    """entries 是 (该笔金额的标的币种数额, 其汇率, is_stale)，陈旧的那笔不参与平均。"""
    entries = [(100.0, 7.0, False), (100.0, 8.0, True)]
    assert weighted_rate(entries) == pytest.approx(7.0)


def test_weighted_rate_weights_by_amount_not_by_count() -> None:
    entries = [(100.0, 7.0, False), (300.0, 7.5, False)]
    assert weighted_rate(entries) == pytest.approx(7.375)


def test_the_two_pieces_always_add_up_to_the_reported_change() -> None:
    """§5.4 line 434 的恒等式：合起来必须精确等于报告币种里的市值−成本。

    这是整个账本唯一能被机器证伪的地方，所以每个测试参数都要走一遍它。
    """
    pnl = position_pnl(qty=100, avg_cost=10.0, last_price=12.0, rate_base=7.0, rate_today=7.2)
    assert pnl.price_pnl == pytest.approx(200.0)  # 标的币种
    assert pnl.fx_pnl == pytest.approx(240.0)  # 报告币种
    assert pnl.price_pnl * 7.0 + pnl.fx_pnl == pytest.approx(
        100 * 12.0 * 7.2 - 100 * 10.0 * 7.0
    )


def test_no_rate_means_no_fx_split() -> None:
    """报告币种与标的币种一致时 rate 传 None：此时 fx_pnl 是 0，不是缺数。"""
    pnl = position_pnl(qty=10, avg_cost=5.0, last_price=6.0, rate_base=None, rate_today=None)
    assert pnl.fx_pnl == 0.0
    assert pnl.price_pnl == pytest.approx(10.0)
    assert pnl.total_report == pytest.approx(10.0)  # 60 − 50，同币种不换算


def test_a_stale_rate_is_visible_on_the_result() -> None:
    """汇率陈旧（周末/停牌）要在结果上留痕：否则周五的汇率配周一的股价就是假数。"""
    pnl = position_pnl(
        qty=1, avg_cost=10.0, last_price=11.0, rate_base=7.0, rate_today=7.0,
        rate_today_stale=True,
    )
    assert pnl.fx_pnl == 0.0
    assert pnl.rate_stale is True


def test_stale_window_is_a_constant_someone_can_argue_with() -> None:
    assert FX_STALE_DAYS == 4


def test_amount_carries_its_currency() -> None:
    """§5.4 硬规则 3：金额阈值必须带币种。裸数字在两个账户之间没有意义。"""
    assert money_note(1234.5, "CNY") == "¥1234.50"
    assert money_note(1234.5, "USD") == "$1234.50"
    assert money_note(1234.5, "HKD") == "HK$1234.50"
    assert money_note(1234.5, "XXX") == "1234.50 XXX"


def test_position_pnl_is_a_frozen_record() -> None:
    """读用例到处携带它；可变的话「先打印后改」这种 bug 无从定位。"""
    import dataclasses

    with pytest.raises(dataclasses.FrozenInstanceError):
        pnl = position_pnl(qty=1, avg_cost=1.0, last_price=1.0, rate_base=None, rate_today=None)
        pnl.qty = 2  # type: ignore[misc]
    assert PositionPnl.__dataclass_params__.frozen
