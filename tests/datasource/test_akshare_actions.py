"""东财分红送配明细 → 公司行为。录制 payload 全部取自 2026-10-18 的真实响应。

这里测的不是「能不能解析」，而是三条会静默出错的口径（模块文档里的那三条）：
每 10 股的单位换算必须有描述列反查、一行方案要拆成多种 kind、未实施的进度不能进库。
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from sams.core.errors import FieldDrift
from sams.datasource.akshare_actions import expand_fhps
from sams.datasource.akshare_cn import AkshareAdapter

# 601088 中国神华 2023 年报方案：10派22.60元(含税,扣税后20.34元)，除权日 2024-07-08。
# 交易所口径的除权参考价 = 前收 45.10 − 每股 2.26 = 42.84，当日实际开盘 42.80——
# 这两个数一起把「单位是每 10 股」钉死：按每股 22.6 元算，参考价会是 22.5 元。
SHENHUA = {
    "报告期": [date(2023, 12, 31)],
    "方案进度": ["实施分配"],
    "股权登记日": [date(2024, 7, 5)],
    "除权除息日": [date(2024, 7, 8)],
    "现金分红-现金分红比例": [22.6],
    "现金分红-现金分红比例描述": ["10派22.60元(含税,扣税后20.34元)"],
    "送转股份-送转总比例": [None],
    "送转股份-送股比例": [None],
    "送转股份-转股比例": [None],
}

# 300059 东方财富 2020 年报方案：10转2.00派0.60元 —— 派息与转增在同一行里（口径 2）
BONUS_ROW = {
    "报告期": [date(2020, 12, 31)],
    "方案进度": ["实施分配"],
    "股权登记日": [date(2021, 5, 26)],
    "除权除息日": [date(2021, 5, 27)],
    "现金分红-现金分红比例": [0.6],
    "现金分红-现金分红比例描述": ["10转2.00派0.60元(含税,扣税后0.54元)"],
    "送转股份-送转总比例": [2.0],
    "送转股份-送股比例": [None],
    "送转股份-转股比例": [2.0],
}

# 神华 2007 年那几行送转给的是 0.0 而不是 NaN：两种写法都得解读成「没有送转」
ZERO_SENT = {**SHENHUA, "送转股份-送转总比例": [0.0], "送转股份-送股比例": [0.0],
             "送转股份-转股比例": [0.0]}


def one(**overrides) -> pl.DataFrame:
    return pl.DataFrame({**SHENHUA, **overrides})


def kinds_and_values(out: pl.DataFrame) -> list[tuple[str, str]]:
    pairs = []
    for row in out.iter_rows(named=True):
        if row["cash_per_share"] is not None:
            pairs.append((row["kind"], f"cash={row['cash_per_share']:.4f}"))
        else:
            pairs.append((row["kind"], f"ratio={row['ratio']:.4f}"))
    return pairs


def test_cash_is_per_share_not_per_ten_shares():
    out = expand_fhps(one())
    assert out["kind"].to_list() == ["dividend"]
    assert out["cash_per_share"].to_list() == [2.26]


def test_zero_and_nan_both_mean_no_share_change():
    assert expand_fhps(one())["ratio"].to_list() == [None]
    assert expand_fhps(one(**ZERO_SENT))["kind"].to_list() == ["dividend"]


def test_one_plan_row_becomes_two_facts():
    out = expand_fhps(pl.DataFrame(BONUS_ROW))
    assert kinds_and_values(out) == [("dividend", "cash=0.0600"), ("transfer", "ratio=1.2000")]
    assert out["ex_date"].to_list() == [date(2021, 5, 27), date(2021, 5, 27)]


def test_bonus_and_transfer_are_recorded_separately():
    """「10送3转增2」是两件事：主键含 kind，同日两条在推导因子时相加（D34）。"""
    out = expand_fhps(
        pl.DataFrame(
            {
                **BONUS_ROW,
                "送转股份-送股比例": [3.0],
                "送转股份-转股比例": [2.0],
                "送转股份-送转总比例": [5.0],
                "现金分红-现金分红比例": [None],
                "现金分红-现金分红比例描述": ["10送3转增2股"],
            }
        )
    )
    assert kinds_and_values(out) == [("bonus", "ratio=1.3000"), ("transfer", "ratio=1.2000")]


def test_only_the_combined_ratio_is_kept_as_bonus_with_the_source_text_attached():
    out = expand_fhps(
        pl.DataFrame(
            {
                **BONUS_ROW,
                "送转股份-送股比例": [None],
                "送转股份-转股比例": [None],
                "现金分红-现金分红比例": [None],
                "现金分红-现金分红比例描述": [None],
            }
        )
    )
    assert kinds_and_values(out) == [("bonus", "ratio=1.2000")]


def test_unimplemented_progresses_are_skipped_not_recorded(caplog):
    """预披露/董事会决议通过/股东大会决议通过都不是「发生过」（口径 3）。"""
    out = expand_fhps(
        one(**{"方案进度": ["预披露"], "除权除息日": [None]}),
    )
    assert out.is_empty()
    out = expand_fhps(pl.DataFrame({**SHENHUA, "方案进度": ["股东大会决议通过"]}))
    assert out.is_empty()
    assert "跳过未实施的公司行为" in caplog.text


def test_implemented_without_ex_date_is_drift():
    """进度说已实施、除权日却是空：这两件事不可能同时为真，多半是列位置错了。"""
    with pytest.raises(FieldDrift, match="为空"):
        expand_fhps(one(**{"除权除息日": [None]}))


def test_description_is_the_cross_check_on_the_unit():
    """数值说 10派22.60，文本说 10 派 2.10 —— 有一列是错的，整批都不能要。"""
    with pytest.raises(FieldDrift, match="不一致"):
        expand_fhps(one(**{"现金分红-现金分红比例描述": ["10派2.10元(含税)"]}))


def test_cash_without_a_readable_description_is_drift():
    """描述列换了格式，单位换算就失去唯一的反查依据——宁可失败，不能猜。"""
    with pytest.raises(FieldDrift, match="无法与描述列核对"):
        expand_fhps(one(**{"现金分红-现金分红比例描述": ["现金分红22.60元/10股"]}))


def test_a_description_written_in_per_share_terms_is_refused_not_cross_checked():
    """「每股派2.26元」能被正则读出一个和数值相等的数，反查会通过——但口径已经换了。

    这正是反查要挡的情形：单位约定一变，乘进因子的数就差十倍，而两边看起来仍然自洽。
    """
    with pytest.raises(FieldDrift, match="每股口径"):
        expand_fhps(one(**{"现金分红-现金分红比例描述": ["每股派2.26元(含税)"]}))


def test_combined_ratio_disagreeing_with_its_parts_is_drift():
    with pytest.raises(FieldDrift, match="送转总比例"):
        expand_fhps(
            pl.DataFrame({**BONUS_ROW, "送转股份-送转总比例": [3.0], "送转股份-送股比例": [None]})
        )


def test_renamed_columns_are_required_not_defaulted():
    """akshare 按位置重命名：接口换字段顺序时这些列会整列消失或错位。"""
    with pytest.raises(FieldDrift, match="缺少必需列"):
        expand_fhps(pl.DataFrame({"除权除息日": [date(2024, 7, 8)]}))


def test_empty_source_answer_expands_to_an_empty_typed_frame():
    out = expand_fhps(pl.DataFrame())
    assert out.is_empty() and out.columns == [
        "ex_date",
        "kind",
        "cash_per_share",
        "ratio",
        "price",
        "note",
    ]


# ------------------------------------------------------------------ 适配器


def stub(payload: pl.DataFrame):
    def fetch(symbol: str) -> pl.DataFrame:
        fetch.symbols = symbol
        return payload

    return fetch


def test_adapter_tags_the_answer_with_the_source_that_gave_it():
    adapter = AkshareAdapter(fhps_fetch=stub(one()))
    out = adapter.corp_actions("A", "601088", date(2024, 1, 1), date(2024, 12, 31))
    assert out["cash_per_share"].to_list() == [2.26]
    assert out["src"].to_list() == ["akshare"]
    assert out["note"].to_list() == ["报告期 2023-12-31；10派22.60元(含税,扣税后20.34元)"]


def multi(ex_dates: list[date], cash_per_ten: list[float]) -> pl.DataFrame:
    """跨多年的分红明细：用来验证全历史源**不**按窗口切片。"""
    return pl.DataFrame(
        {
            "方案进度": ["实施分配"] * len(ex_dates),
            "除权除息日": ex_dates,
            "现金分红-现金分红比例": cash_per_ten,
            "现金分红-现金分红比例描述": [f"10派{v:.2f}元" for v in cash_per_ten],
            "送转股份-送转总比例": [None] * len(ex_dates),
        }
    )


def test_a_full_history_source_answers_the_whole_history_not_the_requested_window():
    """接口一次给全史，就不能只交回窗口那几条。

    服务层按 `corp_actions_full_history` 写下「确认至今天」的覆盖标记，守卫照着它放行
    任意老日子；库里却缺着窗口外那些真的除权日 —— 库与承诺说反话时，2015 年的区间涨跌
    就变成裸价。切片省下的几行行数是 U12 那个错数的价钱。
    """
    payload = multi([date(2015, 7, 8), date(2024, 7, 8)], [8.0, 22.6])
    out = AkshareAdapter(fhps_fetch=stub(payload)).corp_actions(
        "A", "601088", date(2024, 1, 1), date(2024, 3, 1)
    )
    assert out["ex_date"].to_list() == [date(2015, 7, 8), date(2024, 7, 8)]
    assert out["cash_per_share"].to_list() == [0.8, 2.26]


def test_a_stock_that_never_paid_out_answers_empty_not_failed():
    """一次都没有过分红是一家真实存在的公司的正常状态：空帧，让覆盖标记写得下去（D36）。"""
    out = AkshareAdapter(fhps_fetch=stub(pl.DataFrame())).corp_actions(
        "A", "601088", date(2025, 1, 1), date(2025, 6, 30)
    )
    assert out.is_empty() and "cash_per_share" in out.columns


def test_adapter_passes_the_canonical_symbol_and_never_the_users_input():
    """这个接口不回写代码，取错票不会在任何一列上露出破绽——归一必须在这里做掉。"""
    fetch = stub(one())
    adapter = AkshareAdapter(fhps_fetch=fetch)
    adapter.corp_actions("A", "sh601088", date(2024, 1, 1), date(2025, 1, 1))
    assert fetch.symbols == "601088"


def test_adapter_rejects_a_market_it_does_not_serve():
    with pytest.raises(ValueError, match="不支持市场"):
        AkshareAdapter(fhps_fetch=stub(one())).corp_actions(
            "HK", "00700", date(2024, 1, 1), date(2025, 1, 1)
        )


@pytest.mark.live
def test_live_derived_reference_price_matches_the_open_on_ex_date():
    """真接口 + 真行情对齐交易所口径的除权参考价。

    这条证明的不是「我们能取到数」，而是「我们对每10股口径的理解还没过期」：
    单位错一位，参考价会差一个数量级，当日开盘价就在 ±10% 涨跌幅之外。
    """
    from sams.analysis.adjust import ex_reference
    from sams.datasource.tencent import TencentAdapter

    adapter = AkshareAdapter()
    actions = adapter.corp_actions("A", "601088", date(2024, 7, 1), date(2024, 7, 31))
    cash = actions["cash_per_share"][0]
    assert abs(cash - 2.26) < 0.01, f"神华 2024 年方案是 10派22.60元，读出每股 {cash} 元"

    bars = TencentAdapter().daily_bars("A", "601088", date(2024, 7, 1), date(2024, 7, 12))
    prev_close = float(bars.filter(pl.col("date") == date(2024, 7, 5))["close"][0])
    opened = float(bars.filter(pl.col("date") == date(2024, 7, 8))["open"][0])
    reference = ex_reference(prev_close, cash=cash, paid=0.0, share_multiplier=1.0)
    assert abs(reference - opened) / opened < 0.02, f"参考价 {reference} vs 开盘 {opened}"
