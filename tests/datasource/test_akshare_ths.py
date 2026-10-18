"""同花顺分红送配 → 标准列。用例里的每一条方案文本都是从真实响应里抄的样式。

东财那张 `RPT_SHAREBONUS_DET` 对 002450/600636 这类票直接回「返回数据为空」，而同花顺有
28/10 条除权记录（交易员试用报告 P1）。这个源就是补那个洞的：它不是「备份」，它是这些票
唯一的记录来源。

同花顺与东财的口径差别决定了解析的形状：东财给数值列（可以拿描述列反查），**同花顺只给
一句方案说明**——没有第二个数可核对，所以说明本身必须逐样式验过，认不出来的一律按 D39
记 `unknown`（让跨那天的区间拒绝出数），而不是猜一个比例乘进因子。
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from sams.core.errors import FieldDrift
from sams.datasource.akshare_ths import AkshareThsAdapter, expand_ths
from sams.datasource.sources import catalog

_COLUMNS = ("报告期", "方案进度", "分红方案说明", "A股除权除息日")


def raw(*rows, columns=_COLUMNS) -> pl.DataFrame:
    """一行方案：`(报告期, 进度, 说明, 除权日)`。"""
    return pl.DataFrame([dict(zip(columns, row, strict=True)) for row in rows])


def implemented(text: str, ex: date | None = date(2011, 6, 10)) -> pl.DataFrame:
    return raw(("2010年报", "实施方案", text, ex))


def pick(frame: pl.DataFrame, kind: str) -> dict:
    rows = frame.filter(pl.col("kind") == kind).to_dicts()
    assert len(rows) == 1, f"{kind} 应当恰好一行，实际 {rows}"
    return rows[0]


# --- 样式：实测出现过的写法（同花顺不给数值列，说明文本就是唯一口径） ----------------


def test_cash_only():
    """`10派1.8元(含税)` ⇒ 每股 0.18。含税口径：交易所的除权参考价按税前算。"""
    out = expand_ths(implemented("10派1.8元(含税)"))
    assert pick(out, "dividend")["cash_per_share"] == pytest.approx(0.18)


def test_the_after_tax_number_in_parentheses_is_not_the_one_we_want():
    """`10派22.60元(含税,扣税后20.34元)` 只能取 22.60：取到 20.34 会把因子算低一截。"""
    out = expand_ths(implemented("10派22.60元(含税,扣税后20.34元)"))
    assert pick(out, "dividend")["cash_per_share"] == pytest.approx(2.26)


def test_transfer_and_cash_in_one_scheme_are_two_facts():
    """002450 的 2010 年报真实方案 `10转10股派0.45元(含税)`：转增与派息各一行。"""
    out = expand_ths(implemented("10转10股派0.45元(含税)", date(2011, 6, 10)))
    assert pick(out, "transfer")["ratio"] == pytest.approx(2.0)
    assert pick(out, "dividend")["cash_per_share"] == pytest.approx(0.045)
    assert out["ex_date"].to_list() == [date(2011, 6, 10)] * 2


def test_bonus_and_transfer_are_separate_rows():
    """`10送1.2转增0.8股`：送与转的价内语义不同（D34 分成两行，因子按新增股数相加）。"""
    out = expand_ths(implemented("10送1.2转增0.8股"))
    assert pick(out, "bonus")["ratio"] == pytest.approx(1.12)
    assert pick(out, "transfer")["ratio"] == pytest.approx(1.08)


def test_the_cash_can_come_first_in_the_sentence():
    """`10派2元送5股`——说明里没有固定的先后，按位置取数会把 2 当成送股。"""
    out = expand_ths(implemented("10派2元送5股"))
    assert pick(out, "dividend")["cash_per_share"] == pytest.approx(0.2)
    assert pick(out, "bonus")["ratio"] == pytest.approx(1.5)


def test_the_ten_shares_can_be_written_before_the_verb():
    """`10股派1元(含税)`：基数与动词之间夹了个「股」。"""
    out = expand_ths(implemented("10股派1元(含税)"))
    assert pick(out, "dividend")["cash_per_share"] == pytest.approx(0.1)


def test_full_width_parentheses():
    """`10转5股派1.5元（含税）`——同一份表里两种括号都出现过。"""
    out = expand_ths(implemented("10转5股派1.5元（含税）"))
    assert pick(out, "transfer")["ratio"] == pytest.approx(1.5)
    assert pick(out, "dividend")["cash_per_share"] == pytest.approx(0.15)


def test_the_a_share_clause_of_an_ab_share_scheme():
    """`A股10送3.5股派1.5元,B股10送2.426股派1.04元`：取标了 A股的那段。

    取错段的后果是乘进一个 B 股比例，而它和 A 股的比例本来就差 30%——两个数都看着正常。
    """
    out = expand_ths(implemented("A股10送3.5股派1.5元,B股10送2.426股派1.04元"))
    assert pick(out, "bonus")["ratio"] == pytest.approx(1.35)
    assert pick(out, "dividend")["cash_per_share"] == pytest.approx(0.15)


def test_every_ten_shares_prefix_is_accepted():
    out = expand_ths(implemented("每10股派1.5元(含税)"))
    assert pick(out, "dividend")["cash_per_share"] == pytest.approx(0.15)


def test_a_different_base_scales_the_number_by_that_base():
    """基数写在文本里就按文本算：`20派2元` 是每股 0.1，不是 0.2。"""
    out = expand_ths(implemented("20派2元(含税)"))
    assert pick(out, "dividend")["cash_per_share"] == pytest.approx(0.1)


# --- 什么算事实 -------------------------------------------------------------------


def test_only_implemented_schemes_become_facts():
    """`董事会预案`/`股东大会预案`/`取消分红` 都没发生过；乘进序列比漏一次更难被发现。"""
    out = expand_ths(
        raw(
            ("2024年报", "董事会预案", "10派1元(含税)", None),
            ("2024年报", "股东大会预案", "10派1元(含税)", None),
            ("2024年报", "取消分红", "10派1元(含税)", None),
        )
    )
    assert out.is_empty()


def test_no_distribution_is_not_a_fact():
    assert expand_ths(implemented("不分配不转增")).is_empty()


def test_a_missing_ex_date_is_dropped_because_it_cannot_be_placed():
    """实测 600585 有一行 `实施方案` 却没有除权日：主键需要日期，猜一天等于编一次除权。"""
    out = expand_ths(implemented("10派0.5元(含税)", None))
    assert out.is_empty()


def test_an_unreadable_scheme_refuses_to_be_a_number_and_is_recorded_unknown():
    """D39：认不出来的方案留下 `unknown`，跨那天的区间因此拒绝出数而不是给错数。"""
    out = expand_ths(implemented("每10股派发1股实物资产"))
    assert pick(out, "unknown")["cash_per_share"] is None
    assert "每10股派发1股实物资产" in pick(out, "unknown")["note"]


def test_a_scheme_with_no_base_at_all_is_unknown_not_a_guess():
    """`派1元` 没说每几股：按 10 除是猜，猜错一个数量级的因子照样单调。"""
    out = expand_ths(implemented("派1元(含税)"))
    assert pick(out, "unknown")["ratio"] is None


# --- 库里那一行 -------------------------------------------------------------------


def test_note_carries_the_report_period_and_the_source_wording():
    """留原文是为了让人能核对公告：数值列一旦被怀疑，唯一能说清源原话的就是这格。"""
    note = expand_ths(implemented("10派1.8元(含税)"))["note"].to_list()[0]
    assert "2010年报" in note and "10派1.8元(含税)" in note


def test_the_identical_row_repeated_by_the_source_is_counted_once():
    """同一方案在两个公告阶段各出现一次时不能相加：D41 的相加针对的是**不同**事实。"""
    out = expand_ths(
        raw(
            ("2010年报", "实施方案", "10派1.8元(含税)", date(2011, 6, 10)),
            ("2010年报", "实施方案", "10派1.8元(含税)", date(2011, 6, 10)),
        )
    )
    assert out.height == 1


def test_two_different_schemes_on_the_same_day_are_both_kept():
    """年度分红与特别分红同日除权是真实事件，合成一行会少乘一次（D41）。"""
    out = expand_ths(
        raw(
            ("2010年报", "实施方案", "10派1元(含税)", date(2011, 6, 10)),
            ("2010中期", "实施方案", "10派0.5元(含税)", date(2011, 6, 10)),
        )
    )
    assert out.filter(pl.col("kind") == "dividend").sort("cash_per_share")[
        "cash_per_share"
    ].to_list() == pytest.approx([0.05, 0.1])


def test_required_columns_are_checked_as_a_batch():
    """按位置重命名的列一旦错位，`A股除权除息日` 里可能装着金额——整批失败比逐行猜安全。"""
    with pytest.raises(FieldDrift, match="缺少必需列"):
        expand_ths(pl.DataFrame({"报告期": ["2010年报"]}))


def test_empty_input_gives_the_empty_standard_frame_with_the_right_schema():
    out = expand_ths(pl.DataFrame(schema={c: pl.Utf8 for c in _COLUMNS}))
    assert out.columns == [
        "ex_date",
        "kind",
        "cash_per_share",
        "ratio",
        "price",
        "note",
    ]
    assert out.is_empty()


# --- 适配器与路由 -----------------------------------------------------------------


def adapter_with(frame: pl.DataFrame) -> AkshareThsAdapter:
    return AkshareThsAdapter(plans_fetch=lambda symbol: frame)


def test_the_adapter_is_registered_with_only_the_actions_capability():
    """能力决定它能不能被算成 A 股日线的备源（D38）：它拉不到价格。"""
    entry = catalog().by_kind()["akshare_ths"]
    assert entry.markets == frozenset({"A"})
    assert entry.capabilities == ("corp_actions",)


def test_a_full_history_answer_is_not_sliced_to_the_window():
    """D40：请求 2024 年三个月，源给了 2011 年的除权也得交回去——窗口外那行是真事实。"""
    out = adapter_with(
        raw(("2010年报", "实施方案", "10转10股派0.45元(含税)", date(2011, 6, 10)))
    ).corp_actions("A", "002450", date(2024, 1, 1), date(2024, 3, 1))
    assert out["ex_date"].to_list() == [date(2011, 6, 10), date(2011, 6, 10)]
    assert set(out["kind"].to_list()) == {"dividend", "transfer"}
    assert set(out["src"].to_list()) == {"akshare_ths"}


def test_a_stock_the_source_has_nothing_for_answers_empty_not_failed():
    """「查过了、没有」是一句答复；把它当故障换源，覆盖标记就永远写不上（D36）。"""
    out = adapter_with(pl.DataFrame(schema={c: pl.Utf8 for c in _COLUMNS})).corp_actions(
        "A", "002450", date(2024, 1, 1), date(2024, 3, 1)
    )
    assert out.is_empty()
    assert out.columns[:2] == ["market", "code"]


def test_an_unknown_scheme_survives_to_the_repository_with_the_source_wording():
    """D39 走完整条路：折不出数的那行必须带着原文入库，而不是在规范化里被丢掉。"""
    out = adapter_with(
        raw(("2023年报", "实施方案", "每10股派发1股实物资产", date(2024, 7, 8)))
    ).corp_actions("A", "600000", date(2024, 1, 1), date(2024, 7, 1))
    assert out["kind"].to_list() == ["unknown"]
    assert "实物资产" in out["note"].to_list()[0]


def test_a_market_this_adapter_does_not_serve_is_refused_before_the_call():
    with pytest.raises(ValueError, match="不支持市场"):
        AkshareThsAdapter(plans_fetch=lambda symbol: raw()).corp_actions(
            "HK", "00700", date(2024, 1, 1), date(2024, 3, 1)
        )


@pytest.mark.live
def test_live_002450_has_the_records_eastmoney_says_do_not_exist():
    """002450 是那条 P1 的当事票：东财回「返回数据为空」，同花顺有整摞除权记录。

    录制样本会跟着接口改版一起失效，所以这条只在真接口上验形状。
    """
    from sams.core.clock import SYSTEM_CLOCK

    out = AkshareThsAdapter().corp_actions("A", "002450", date(2020, 1, 1), SYSTEM_CLOCK.today())
    assert out.height > 5, "同花顺对 002450 也只给 0 条 = 这个备源没补上洞"
    assert set(out["kind"].to_list()) & {"dividend", "bonus", "transfer"}
    assert {a["ex_date"] for a in out.to_dicts()} == set(out["ex_date"].to_list())
