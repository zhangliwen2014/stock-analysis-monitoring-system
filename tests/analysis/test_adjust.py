"""因子推导的黄金样例（D34 / D35）。

每条都用手算值对齐。复权是唯一一处「错了也不会报错」的逻辑：价格序列照样画得出图，
指标照样算得出来，只有区间收益悄悄偏掉——所以基准必须是手算，不是另一个来源给的数。
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from sams.analysis.adjust import day_factors, group_by_ex_date, hfq_frame
from sams.core.errors import NotAdjustable

D0 = date(2024, 7, 5)  # 除权前一天（周五）
D1 = date(2024, 7, 8)  # 除权日（下周一）
D2 = date(2024, 7, 9)
D3 = date(2024, 7, 10)
#: K 线只能落在这些天上。用真实的交易日序列而不是连续自然日，是因为周末会让
#: 「除权日前一根」与「除权日」在数据里差三天——因子对不对看不出来，序列断没断才看得出来
DAYS = [D0, D1, D2, D3]

_COLUMNS = ("ex_date", "kind", "cash_per_share", "ratio", "price")


def bars(*closes, first: int = 0) -> pl.DataFrame:
    return pl.DataFrame({"date": DAYS[first : first + len(closes)], "close": list(closes)})


def actions(*rows) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame(
            schema={
                "ex_date": pl.Date,
                "kind": pl.Utf8,
                "cash_per_share": pl.Float64,
                "ratio": pl.Float64,
                "price": pl.Float64,
            }
        )
    return pl.DataFrame([dict(zip(_COLUMNS, row, strict=True)) for row in rows])


# --- 五种行为各一条手算 ---------------------------------------------------------------


def test_cash_dividend_only():
    """前收 100、每股派 1 → 参考价 99，因子 100/99。除权后价格 99 复权回 100：
    钱拿到了，图上看没跌——这正是复权要表达的事。"""
    frame = hfq_frame(bars(100.0, 99.0), actions((D1, "dividend", 1.0, None, None)))
    factor = frame["adj_factor"].to_list()
    assert factor[0] == pytest.approx(1.0), "除权日之前的价格不动"
    assert factor[1] == pytest.approx(100 / 99)
    assert frame["close_hfq"].to_list() == pytest.approx([100.0, 100.0])


def test_bonus_shares_only():
    """10 送 5 → 股数乘数 1.5，因子 1/1.5，与前收无关。"""
    frame = hfq_frame(bars(100.0, 66.67), actions((D1, "bonus", None, 1.5, None)))
    assert frame["adj_factor"].to_list()[1] == pytest.approx(1.5)
    assert frame["close_hfq"].to_list()[1] == pytest.approx(100.005)


def test_split_and_reverse_split():
    """1 拆 4 是 ratio=4，2 合 1 是 ratio=0.5。写成「每股变 0.25 股」是另一种语义。"""
    split = hfq_frame(bars(100.0, 25.0), actions((D1, "split", None, 4.0, None)))
    assert split["close_hfq"].to_list() == pytest.approx([100.0, 100.0])
    merged = hfq_frame(bars(25.0, 50.0), actions((D1, "split", None, 0.5, None)))
    assert merged["close_hfq"].to_list() == pytest.approx([25.0, 25.0])


def test_rights_issue_needs_the_issue_price():
    """10 配 2 每股 8.7 元：参考价 (100 + 8.7×0.2)/1.2 = 84.7833。

    没有配股价就推不出这个数——只记「10 配 2」等于记了一半事实，
    而漏掉的那一半恰好是钱。
    """
    frame = hfq_frame(bars(100.0, 84.78), actions((D1, "rights", None, 1.2, 8.7)))
    day = day_factors(bars(100.0, 84.78), actions((D1, "rights", None, 1.2, 8.7)))[0]
    assert day.ex_ref == pytest.approx((100 + 8.7 * 0.2) / 1.2)
    assert frame["adj_factor"].to_list()[1] == pytest.approx(100 / day.ex_ref)


def test_dividend_and_bonus_on_the_same_day_merge_into_one_factor():
    """同一天既派息又送股很常见（10 派 3 送 2）。两件行为合成一个因子，不连乘两次。"""
    both = actions(
        (D1, "dividend", 0.3, None, None),
        (D1, "bonus", None, 1.2, None),
    )
    day = day_factors(bars(100.0, 83.0), both)[0]
    assert len(group_by_ex_date(both)) == 1, "同一天压成一条，不然会算两次"
    assert day.ex_ref == pytest.approx((100 - 0.3) / 1.2)


def test_bonus_plus_transfer_add_shares_not_multiply():
    """10 送 3 转 2 是 1.5 倍股数，不是 1.3 × 1.2。"""
    both = actions(
        (D1, "bonus", None, 1.3, None),
        (D1, "transfer", None, 1.2, None),
    )
    day = day_factors(bars(100.0, 66.0), both)[0]
    assert day.ex_ref == pytest.approx(100 / 1.5)


# --- 拒绝出数的三种情形 ----------------------------------------------------------------


def test_cash_dividend_without_a_previous_bar_is_refused():
    """库里只有除权日之后的价格时，参考价无从折算。

    给 1.0 会让这一天看起来「没除权」，而它明明派了钱——这正是 U12 要防的错法。
    """
    with pytest.raises(NotAdjustable, match="前一根"):
        hfq_frame(bars(99.0, 98.0, first=1), actions((D1, "dividend", 1.0, None, None)))


def test_a_share_count_multiplier_of_zero_is_refused():
    with pytest.raises(NotAdjustable, match="自相矛盾"):
        hfq_frame(bars(100.0, 1.0), actions((D1, "split", None, 0.0, None)))


def test_a_dividend_bigger_than_the_previous_close_is_refused():
    """参考价算出非正数 = 这条记录不可信，硬算会得到负因子并把整段序列翻到零以下。"""
    with pytest.raises(NotAdjustable, match="非正"):
        hfq_frame(bars(1.0, 0.5), actions((D1, "dividend", 2.0, None, None)))


def test_no_corp_actions_leaves_the_series_untouched():
    frame = hfq_frame(bars(100.0, 99.0), actions())
    assert frame["close_hfq"].to_list() == pytest.approx([100.0, 99.0])
    assert frame["adj_factor"].to_list() == pytest.approx([1.0, 1.0])


def test_factors_accumulate_over_several_ex_dates():
    """两次派息各 1 元，价格 100 → 99 → 98：复权后必须一路是 100。"""
    frame = hfq_frame(
        bars(100.0, 99.0, 98.0, 97.0),
        actions(
            (D1, "dividend", 1.0, None, None),
            (D2, "dividend", 1.0, None, None),
        ),
    )
    # 97 是真的跌了一元（98→97），不是又一次除权：复权后按 100 的基准读作 −1.02%
    assert frame["close_hfq"].to_list() == pytest.approx([100.0, 100.0, 100.0, 98.9796], abs=1e-4)


def test_an_unpriceable_action_refuses_the_whole_series():
    """D39：折不出数的公司行为（港股实物分派这类）不能当没发生过。

    少乘一次除权在图上看不出来，而港美股没有涨跌停证据规则（D37）可兜——这条拒绝
    就是唯一那道闸。
    """
    with pytest.raises(NotAdjustable, match="以股代息"):
        hfq_frame(bars(100.0, 99.0), actions((D1, "unknown", None, None, None))
                  .with_columns(pl.lit("以股代息，比例未披露").alias("note")))


def test_a_known_kind_still_derives_a_factor():
    """拒绝只认 unknown：其余类型照常推导，别把整条链一起挡死。"""
    frame = hfq_frame(bars(100.0, 99.0), actions((D1, "dividend", 1.0, None, None)))
    assert frame["adj_factor"].to_list() == pytest.approx([1.0, 100.0 / 99.0])
