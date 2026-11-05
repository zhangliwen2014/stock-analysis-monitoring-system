"""新浪财务指标（`stock_financial_analysis_indicator`）→ `FINANCIAL_COLUMNS`（#387 / D324）。

夹具是 11-06 实网录下的原始列名与两行（600519 的 2023Q1、2018 年报）。接它是因为
A 股基本面原来只有一个源（东财），而 11-05 那一晚东财在 `result: null` 上崩了 38 次
（D327）——链上再没有第二个源可换。

三件必须钉住的事：

* **百分数只除一次**（D58）：ROE 10.00 → 0.10，增速 20.0028 → 0.200028；
* `股息发放率(%)` **不是股息率**：它是分红 ÷ 净利（payout ratio），映射进
  `dividend_yield` 会让高分红票的股息率凭空变成 30%——方向还正好是「看起来更便宜」；
* 这个源**不给披露日**（`published_date` 全 NULL），也不给营收/净利金额。

| 列 | 标准列 | 2023Q1 | 2018 年报 |
|---|---|---|---|
| 摊薄每股收益(元) | eps | 17.1348 | 30.1144 |
| 每股净资产_调整前(元) | bps | 180.2770 | 93.4634 |
| 每股经营性现金流(元) | ocf_per_share | 4.1751 | 32.9448 |
| 加权净资产收益率(%) | roe | 10.00 | 34.46 |
| 主营业务收入增长率(%) | revenue_yoy | 20.0028 | 26.4885 |
| 净利润增长率(%) | profit_yoy | 19.9008 | 30.4181 |
| 销售毛利率(%) | gross_margin | — | 91.142 |
| 销售净利率(%) | net_margin | 55.5393 | 51.3718 |
| 总资产净利润率(%) | roa | 8.3957 | 25.6945 |
| 资产负债率(%) | debt_ratio | 12.3562 | 26.5493 |
| 流动比率 | current_ratio | 6.9427 | 3.2485 |
| 速动比率 | quick_ratio | 5.6683 | 2.6946 |
| 存货周转率(次) | inventory_turnover | 0.0728 | 0.2863 |
| 总资产周转率(次) | total_asset_turnover | 0.1512 | 0.5002 |
| 股息发放率(%) | **不映射** | — | — |
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from sams.core.errors import FieldDrift, TransientError
from sams.core.fundamentals import FINANCIAL_COLUMNS
from sams.datasource.sina import SinaAdapter, parse_sina_financials

Q1_2023 = date(2023, 3, 31)
FY_2018 = date(2018, 12, 31)

#: 2023Q1 与 2018 年报的真实读数；只留映射到的列 + 两列「不该被映射」的邻居
RECORDED = [
    {
        "日期": Q1_2023,
        "摊薄每股收益(元)": 17.1348,
        "每股净资产_调整前(元)": 180.2770,
        "每股经营性现金流(元)": 4.1751,
        "加权净资产收益率(%)": 10.00,
        "主营业务收入增长率(%)": 20.0028,
        "净利润增长率(%)": 19.9008,
        "销售毛利率(%)": None,
        "销售净利率(%)": 55.5393,
        "总资产净利润率(%)": 8.3957,
        "资产负债率(%)": 12.3562,
        "流动比率": 6.9427,
        "速动比率": 5.6683,
        "存货周转率(次)": 0.0728,
        "总资产周转率(次)": 0.1512,
        "股息发放率(%)": None,
        "三项费用比重": 6.0594,
    },
    {
        "日期": FY_2018,
        "摊薄每股收益(元)": 30.1144,
        "每股净资产_调整前(元)": 93.4634,
        "每股经营性现金流(元)": 32.9448,
        "加权净资产收益率(%)": 34.46,
        "主营业务收入增长率(%)": 26.4885,
        "净利润增长率(%)": 30.4181,
        "销售毛利率(%)": 91.142,
        "销售净利率(%)": 51.3718,
        "总资产净利润率(%)": 25.6945,
        "资产负债率(%)": 26.5493,
        "流动比率": 3.2485,
        "速动比率": 2.6946,
        "存货周转率(次)": 0.2863,
        "总资产周转率(次)": 0.5002,
        # 55% 的分红率：映射进 dividend_yield 会把股息率写成 0.55
        "股息发放率(%)": 55.0,
        "三项费用比重": 8.7411,
    },
]


def raw(rows=RECORDED) -> pl.DataFrame:
    return pl.DataFrame(rows, schema_overrides={"日期": pl.Date})


def parse(rows=RECORDED, start=date(2000, 1, 1), end=date(2026, 11, 6), code="600519"):
    return parse_sina_financials(raw(rows), code=code, start=start, end=end)


def test_recorded_payload_maps_every_column_into_the_standard_schema():
    out = parse()
    # 源按日期升序交回；解析保持同一顺序，重复报告期后写覆盖
    assert out["report_date"].to_list() == [FY_2018, Q1_2023]
    assert out["eps"].to_list() == pytest.approx([30.1144, 17.1348])
    assert out["bps"].to_list() == pytest.approx([93.4634, 180.2770])
    assert out["ocf_per_share"].to_list() == pytest.approx([32.9448, 4.1751])
    assert out["source"].unique().to_list() == ["sina"]
    assert out["market"].unique().to_list() == ["A"]
    assert out["code"].unique().to_list() == ["600519"]


def test_percentages_are_divided_exactly_once():
    out = parse()
    assert out["roe"].to_list() == pytest.approx([0.3446, 0.10])
    assert out["revenue_yoy"].to_list() == pytest.approx([0.264885, 0.200028])
    assert out["profit_yoy"].to_list() == pytest.approx([0.304181, 0.199008])
    assert out["net_margin"].to_list() == pytest.approx([0.513718, 0.555393])
    assert out["roa"].to_list() == pytest.approx([0.256945, 0.083957])
    assert out["debt_ratio"].to_list() == pytest.approx([0.265493, 0.123562])
    margin = out["gross_margin"].to_list()
    assert margin[0] == pytest.approx(0.91142)
    assert margin[1] is None, "这一期源就是空值：不是 0，也不该被填成 0"


def test_multiples_and_per_share_values_are_not_divided():
    out = parse()
    # 流动比率/速动比率/周转率是倍或次，÷100 会把 6.94 写成 0.0694
    assert out["current_ratio"].to_list() == pytest.approx([3.2485, 6.9427])
    assert out["quick_ratio"].to_list() == pytest.approx([2.6946, 5.6683])
    assert out["inventory_turnover"].to_list() == pytest.approx([0.2863, 0.0728])
    assert out["total_asset_turnover"].to_list() == pytest.approx([0.5002, 0.1512])


def test_payout_ratio_is_not_a_dividend_yield():
    """`股息发放率` 是分红 ÷ 净利。存成股息率会让这只票在「高股息」筛里凭空出现。"""
    out = parse()
    assert out["dividend_yield"].to_list() == [None, None]


def test_this_source_gives_no_publication_date_and_no_money_columns():
    out = parse()
    # 不给披露日：NULL 进库，A 股的 PIT 读取会按 D61 挡掉它，不会当成「已公开」
    assert out["published_date"].to_list() == [None, None]
    for column in ("revenue", "net_profit", "market_cap", "roic"):
        assert out[column].to_list() == [None, None]


def test_rows_outside_the_requested_window_are_dropped():
    out = parse(start=date(2023, 1, 1))
    assert out["report_date"].to_list() == [Q1_2023]


def test_a_missing_ratio_column_is_drift_not_a_null_column():
    """`加权净资产收益率(%)` 整列消失 = 接口改版；填 NULL 继续走会写出一库空的 ROE。"""
    rows = [{k: v for k, v in RECORDED[0].items() if k != "加权净资产收益率(%)"}]
    with pytest.raises(FieldDrift, match="加权净资产收益率"):
        parse(rows)


def test_an_empty_answer_keeps_the_full_schema():
    out = parse_sina_financials(
        pl.DataFrame(), code="600519", start=date(2024, 1, 1), end=date(2026, 11, 6)
    )
    assert out.is_empty()
    assert out.columns == list(FINANCIAL_COLUMNS)


class FakeFetch:
    def __init__(self, frame: pl.DataFrame | Exception):
        self.frame = frame
        self.calls: list[tuple[str, str]] = []

    def __call__(self, symbol: str, start_year: str) -> pl.DataFrame:
        self.calls.append((symbol, start_year))
        if isinstance(self.frame, Exception):
            raise self.frame
        return self.frame


def test_the_adapter_asks_the_bare_code_for_the_start_year():
    fetch = FakeFetch(raw())
    frame = SinaAdapter(financials_fetch=fetch).financials(
        "A", "600519", date(2023, 1, 1), date(2023, 12, 31)
    )
    # 财务页用的是 6 位裸代码；`sh600519` 会打开一张空页
    assert fetch.calls == [("600519", "2023")]
    assert frame["report_date"].to_list() == [Q1_2023]


def test_the_adapter_answers_other_markets_with_a_value_error():
    with pytest.raises(ValueError, match="不支持市场"):
        SinaAdapter(financials_fetch=FakeFetch(raw())).financials(
            "HK", "00700", date(2024, 1, 1), date(2025, 1, 1)
        )


def test_a_network_failure_is_classified_not_swallowed():
    adapter = SinaAdapter(financials_fetch=FakeFetch(ConnectionError("reset by peer")))
    with pytest.raises(TransientError):
        adapter.financials("A", "600519", date(2024, 1, 1), date(2025, 1, 1))
