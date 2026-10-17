"""代码归一与币种例外（spec §5.4）。

这两个函数决定「一只票在库里只有一个键、以什么币种记账」，写错的后果都不是报错，
而是同一只票长出两条曲线、或一条曲线被当成另一种钱。
"""

from __future__ import annotations

import pytest

from sams.core.codes import canonical_code, currency_override, lot_size_of


@pytest.mark.parametrize(
    "raw",
    ["700", "00700", "HK00700", "hk.00700", "00700.HK", " HK00700 ", "HK.00700"],
)
def test_every_hk_spelling_lands_on_one_key(raw):
    """清单里写 `700`、库里存 `00700`：那一行永远读不到行情，而且不报错。"""
    assert canonical_code("HK", raw) == "00700"


@pytest.mark.parametrize("raw", ["601088", "601088.SH", "601088.SS", "000001.SZ", " 601088 "])
def test_a_share_drops_the_exchange_suffix_only(raw):
    for value in (raw, raw.lower(), f" {raw} "):
        assert canonical_code("A", value) == raw.strip().split(".")[0]


@pytest.mark.parametrize("raw", ["sh601088", "SH601088", "sz000001", "bj830799"])
def test_a_share_drops_the_exchange_prefix_too(raw):
    """新浪/腾讯形态的前缀也是别名。留着它，只认 6 位数字的东财接口会回「没有记录」——
    症状不是报错，而是「这只票从没分过红」，比多一个主键更难查。"""
    assert canonical_code("A", raw) == raw[2:]


def test_an_alphabetic_ticker_is_never_mistaken_for_a_prefixed_code():
    """NYSE:SH（中石化上海）剥掉 `SH` 会变成空串；`SHLX` 会变成 `LX`。
    只在剩余部分全是数字时才剥前缀，就是为了不把美股符号啃掉一块。"""
    assert canonical_code("US", "sh") == "SH"
    assert canonical_code("US", "SHLX") == "SHLX"


def test_us_ticker_keeps_its_shape_and_uppercases():
    assert canonical_code("US", "brk-b") == "BRK-B"
    assert canonical_code("US", "AAPL") == "AAPL"


def test_a_code_that_is_not_a_number_is_handed_back_untouched():
    """归一不了就原样交出去：把它塞成 `00000` 才是灾难。"""
    assert canonical_code("HK", "HSI") == "HSI"


def test_an_empty_code_is_refused():
    for market in ("A", "HK", "US"):
        with pytest.raises(ValueError, match="不能为空"):
            canonical_code(market, "   ")


# ---------------------------------------------------------------- 币种


@pytest.mark.parametrize("code", ["80700", "81024", "82318", "83690", "89988"])
def test_the_whole_rmb_counter_block_is_priced_in_cny(code):
    """人民币双柜台占用整个 8xxxx 段，不是 80 字头那一小截。

    `startswith("80")` 会漏掉 8 万以后的柜台：同一公司的两条曲线被当成两种币，
    界面上永远显示一个不存在的 7~8% 折价。
    """
    assert currency_override("HK", code) == "CNY"


@pytest.mark.parametrize("code", ["00700", "08000", "08499", "09888"])
def test_ordinary_hk_and_the_growth_board_keep_the_market_default(code):
    """08xxx 是港股通外的创业板，仍以 HKD 报价——它和 8xxxx 只差一个前导 0。"""
    assert currency_override("HK", code) is None


def test_b_shares_are_priced_in_foreign_currency():
    assert currency_override("A", "900901") == "USD"
    assert currency_override("A", "200011") == "HKD"
    assert currency_override("A", "600519") is None


def test_a_code_that_is_not_a_number_gets_no_currency():
    assert currency_override("HK", "HSI") is None


# ---------------------------------------------------------------- 每手股数


def test_lot_size_is_known_where_it_is_a_rule_and_null_where_it_is_per_stock():
    assert lot_size_of("A") == 100
    assert lot_size_of("US") == 1
    # 港股每手逐票由交易所定：存 1 冒充知道，下单换算会错一个数量级
    assert lot_size_of("HK") is None
