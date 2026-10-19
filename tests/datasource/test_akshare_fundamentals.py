"""A 股主源的基本面三链：东财的形状 → 标准列（D58 / D60 / D61 在真实接口上的落点）。

这些用例喂的是**录制 payload**（列名与单位取自 2026-10 实测的 akshare 1.19.1），
所以它们不联网也能跑；联网那一半打了 `live` 标记，形状变了由那一半出声。

三件事必须在这里钉住，每一件都对应一种不会报错的错法：

1. **两个接口的比率单位不一样**。`stock_financial_analysis_indicator_em` 的 `ROEJQ` 按
   百分数给（5.83 = 5.83%），而分红送配表里的 `现金分红-股息率` **本来就是小数**
   （0.055055 = 5.5%）。把它们一起塞进 `percent_columns`，股息率会小一百倍——
   屏幕上那句「股息率 0.06%」看起来像源给的数据，不像 bug。
2. **披露日来自 `NOTICE_DATE`**（D61）。这一列整列丢失时 PIT 读取会把每一期都挡掉，
   症状是「这只票没有已披露的财务数据」，而真相是采集把闸门列扔了。
3. **键形态**。`stock_value_em` 要裸代码、`...indicator_em` 要 `601088.SH`、
   `stock_individual_fund_flow` 要 `market="sh"`——三种写法都是同一个请求的意图，
   写错的那一种不会报错，只会返回另一只票或返回空。
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from sams.core.errors import EmptyResponse, FieldDrift, RateLimited
from sams.core.fundamentals import FINANCIAL_COLUMNS
from sams.datasource.akshare_cn import (
    AkshareAdapter,
    attach_dividend_yield,
    fund_flow_market,
    secucode,
)

START, END = date(2024, 1, 1), date(2024, 6, 30)
#: 装得下两期的窗口：断言「键与来源逐行填对」时用这个，窗口切行由专门的用例管
ALL_START = date(2023, 1, 1)
Q1_2024 = date(2024, 3, 31)
Q4_2023 = date(2023, 12, 31)


def indicators_raw(**over) -> pl.DataFrame:
    """`stock_financial_analysis_indicator_em(symbol="601088.SH", indicator="按报告期")` 的形状。

    只留本系统要用的那几列（源实际给 141 列，全是东财的内部缩写）：日期带 00:00:00，
    比率是百分数。
    """
    row = {
        "SECUCODE": "601088.SH",
        "REPORT_DATE": "2024-03-31 00:00:00",
        "NOTICE_DATE": "2024-04-26 00:00:00",
        "ROEJQ": 5.83,
        "TOTALOPERATEREVETZ": 7.93,
        "PARENTNETPROFITTZ": 4.10,
    }
    row.update(over)
    return pl.DataFrame(
        [
            row,
            {
                "SECUCODE": "601088.SH",
                "REPORT_DATE": "2023-12-31 00:00:00",
                "NOTICE_DATE": "2024-03-31 00:00:00",
                "ROEJQ": 12.76,
                "TOTALOPERATEREVETZ": -13.21,
                "PARENTNETPROFITTZ": -5.30,
            },
        ]
    )


def payouts_raw(**over) -> pl.DataFrame:
    """`stock_fhps_detail_em` 里股息率那两列；**单位是小数**，与上面那个接口相反。"""
    row = {"报告期": "2024-03-31", "现金分红-股息率": 0.0224}
    row.update(over)
    return pl.DataFrame(
        [
            row,
            {"报告期": "2023-12-31", "现金分红-股息率": 0.055055},
        ]
    )


def valuation_raw(**over) -> pl.DataFrame:
    """`stock_value_em` 的列名（中文，PE 那一列的名字带括号）。亏损票的负 PE 是一个真数。"""
    row = {
        "数据日期": "2024-06-28",
        "当日收盘价": 32.1,
        "总市值": 6.4e11,
        "PE(TTM)": 18.5,
        "市净率": 2.1,
    }
    row.update(over)
    return pl.DataFrame(
        [
            row,
            {
                "数据日期": "2024-06-27",
                "当日收盘价": 31.8,
                "总市值": 6.34e11,
                "PE(TTM)": -12.0,
                "市净率": 1.9,
            },
        ]
    )


def flow_raw() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "日期": ["2024-06-28", "2024-06-27"],
            "主力净流入-净额": [1.2e8, -3.4e7],
            "小单净流入-净额": [1.0e7, 2.0e7],
            "收盘价": [32.1, 31.8],
        }
    )


def adapter(**over) -> AkshareAdapter:
    given = {
        "financials_fetch": lambda symbol: indicators_raw(),
        "valuation_fetch": lambda symbol: valuation_raw(),
        "flow_fetch": lambda symbol, market: flow_raw(),
        "fhps_fetch": lambda symbol: payouts_raw(),
    }
    given.update(over)
    return AkshareAdapter(**given)


# ---------- 键形态：同一个意图的三种写法 ----------


@pytest.mark.parametrize(
    ("code", "expected"),
    [("601088", "601088.SH"), ("000001", "000001.SZ"), ("430139", "430139.BJ")],
)
def test_the_secucode_spelling(code, expected):
    assert secucode("A", code) == expected


@pytest.mark.parametrize(
    ("code", "expected"), [("601088", "sh"), ("000001", "sz"), ("830799", "bj")]
)
def test_the_fund_flow_market_tag(code, expected):
    """北交所给 "bj"：把它当深市代码问一次，接口回的是**空**，而空看起来像「这只票没资金流」。"""
    assert fund_flow_market(code) == expected


# ---------- financials：单位、披露日、窗口 ----------


def test_financials_divides_the_percent_feed_and_keeps_the_publication_date():
    out = adapter().financials("A", "601088", ALL_START, END)
    row = out.filter(pl.col("report_date") == Q1_2024)
    assert row["roe"].item() == pytest.approx(0.0583)
    assert row["revenue_yoy"].item() == pytest.approx(0.0793)
    assert row["profit_yoy"].item() == pytest.approx(0.041)
    assert row["published_date"].item() == date(2024, 4, 26), "D61 的闸门列必须是披露日"
    assert out["market"].to_list() == ["A", "A"]
    assert out["code"].to_list() == ["601088", "601088"]
    assert out["source"].to_list() == ["akshare", "akshare"]


def test_the_dividend_yield_from_the_payout_table_is_not_divided_again():
    """两个接口单位相反：股息率那一列已经是小数，再 ÷100 会得到 0.0224%。"""
    out = adapter().financials("A", "601088", START, END)
    row = out.filter(pl.col("report_date") == Q1_2024)
    assert row["dividend_yield"].item() == pytest.approx(0.0224)


def test_financials_window_is_on_the_reporting_period_not_on_the_publication():
    """2023Q4 那期报告期在窗口外、披露日在窗口内：按**报告期**过滤，因为粒度是期（D60）。"""
    out = adapter().financials("A", "601088", START, END)
    assert out["report_date"].to_list() == [Q1_2024]


def test_a_payout_table_without_the_stock_leaves_dividend_yield_unknown():
    """分红表回「查无记录」（空帧）时，股息率是 null 而不是 0：0 会把它读成「不分红」。"""
    out = adapter(fhps_fetch=lambda symbol: pl.DataFrame()).financials("A", "601088", START, END)
    assert out["dividend_yield"].to_list() == [None]


def test_attach_dividend_yield_keeps_the_period_key_and_the_decimal_unit():
    joined = attach_dividend_yield(indicators_raw(), payouts_raw())
    assert joined["股息率"].to_list() == [pytest.approx(0.0224), pytest.approx(0.055055)]


def test_a_renamed_roe_column_is_field_drift_naming_the_missing_one():
    """列名改版不能变成「这只票 ROE 一直是空」：那会一路走到 PR 里说「ROE 缺失」。"""
    with pytest.raises(FieldDrift, match="ROEJQ"):
        adapter(financials_fetch=lambda symbol: indicators_raw().drop("ROEJQ")).financials(
            "A", "601088", START, END
        )


def test_no_disclosure_date_column_is_said_aloud_but_keeps_the_periods(caplog):
    """丢一列披露日不该丢报告期（那些 ROE 是真数），但必须出声：PIT 会全部挡掉。"""
    with caplog.at_level("WARNING"):
        out = adapter(
            financials_fetch=lambda symbol: indicators_raw().drop("NOTICE_DATE")
        ).financials("A", "601088", START, END)
    assert out["published_date"].to_list() == [None]
    assert "NOTICE_DATE" in caplog.text


def test_an_empty_indicator_table_is_a_no_record_answer_not_an_error():
    out = adapter(financials_fetch=lambda symbol: pl.DataFrame()).financials(
        "A", "601088", START, END
    )
    assert out.height == 0
    assert out.columns == list(FINANCIAL_COLUMNS)


# ---------- valuation ----------


def test_valuation_keeps_multiples_as_multiples_and_negative_pe_as_a_number():
    """负 PE 不许置空（D59 靠它分叉）：把它擦成 null，PR 就走不到 PB 那一支，
    而「亏损票算不出 PR」与「亏损票该用 PB 配预估 ROE」是两种完全不同的结论。"""
    out = adapter().valuation("A", "601088", START, END)
    by_day = dict(zip(out["date"], out["pe_ttm"], strict=True))
    assert by_day[date(2024, 6, 28)] == pytest.approx(18.5)
    assert by_day[date(2024, 6, 27)] == pytest.approx(-12.0)
    assert out.filter(pl.col("date") == date(2024, 6, 28))["market_cap"].item() == 6.4e11


def test_valuation_dividend_yield_is_unknown_for_this_feed():
    """东财估值表不给当日股息率：那一列必须是 null，不能拿报告期那个口径来冒充按日。"""
    out = adapter().valuation("A", "601088", START, END)
    assert out["dividend_yield"].to_list() == [None, None]


def test_valuation_drops_rows_outside_the_window():
    out = adapter().valuation("A", "601088", date(2024, 6, 28), date(2024, 6, 28))
    assert out["date"].to_list() == [date(2024, 6, 28)]


def test_valuation_reports_the_code_it_was_asked_for():
    seen = []
    adapter(
        valuation_fetch=lambda symbol: (seen.append(symbol), valuation_raw())[1]
    ).valuation("A", "000001", START, END)
    assert seen == ["000001"], "stock_value_em 要裸代码，带前缀会返回空"


# ---------- funding_flow ----------


def test_flow_takes_the_main_force_column_and_leaves_the_rest_unknown():
    out = adapter().funding_flow("A", "601088", START, END)
    assert out["main_net_in"].to_list() == [pytest.approx(-3.4e7), pytest.approx(1.2e8)]
    for column in ("north_net", "north_hold", "margin_bal"):
        assert out[column].to_list() == [None, None], f"{column} 这个源给不出，填 null 不是 0"


def test_flow_asks_the_exchange_specific_tag():
    seen = {}

    def fetch(symbol, market):
        seen["symbol"] = symbol
        seen["market"] = market
        return flow_raw()

    adapter(flow_fetch=fetch).funding_flow("A", "600519", START, END)
    assert seen == {"symbol": "600519", "market": "sh"}


def test_flow_window_filter_drops_days_outside_the_request():
    out = adapter().funding_flow("A", "601088", date(2024, 6, 28), date(2024, 6, 28))
    assert out["date"].to_list() == [date(2024, 6, 28)]


# ---------- 边界：归类、市场、能力 ----------


def test_a_rate_limited_upstream_is_classified_as_rate_limited():
    def fetch(symbol):
        raise ConnectionError("429 Too Many Requests")

    with pytest.raises(RateLimited):
        adapter(financials_fetch=fetch).financials("A", "601088", START, END)


def test_an_unsupported_market_is_refused_before_the_network():
    with pytest.raises(ValueError, match="HK"):
        adapter().financials("HK", "00700", START, END)


def test_the_adapter_now_declares_three_more_kinds():
    """能力按方法推导（D38）：这几行断言的是「注册表看到的形状」，不是类自己写的声明。

    `listed_date` 也在内：A 股主源顺手把上市日期 surfaced（U18），
    而这一格是 D37 那条豁免唯一的机器来源。
    """
    from sams.datasource.sources import known

    assert known()["akshare"].capabilities == (
        "daily_bars",
        "corp_actions",
        "financials",
        "valuation",
        "funding_flow",
        "listed_date",
    )


def test_an_empty_response_from_the_feed_is_not_rewritten_into_a_no_record_answer():
    """接口回空/解析不出是 `EmptyResponse`（源答不上来），不许被适配器折成 0 行（D43）：
    后者会被降级链读成「这只票在那张表里没记录」，而那只是一次沉默。"""

    def fetch(symbol):
        raise EmptyResponse("akshare", "接口回空")

    with pytest.raises(EmptyResponse):
        adapter(financials_fetch=fetch).financials("A", "601088", START, END)


# ---------- 真实网络：形状与单位（接口改版由这一半出声） ----------


@pytest.mark.live
def test_live_financials_are_decimal_ratios_with_a_publication_date():
    real = AkshareAdapter()
    out = real.financials("A", "601088", date(2023, 1, 1), date(2099, 12, 31))
    assert out.columns == list(FINANCIAL_COLUMNS)
    assert out.height > 0
    roe = [v for v in out["roe"].to_list() if v is not None]
    assert roe, "ROE 一列全空通常意味着列名改了，而不是这家公司不赚钱"
    # 量级断言而不是等值：一个 5.83（百分数没换算）或 0.0000583（多除了一次）都要被抓住
    assert all(-1.0 <= v <= 1.0 for v in roe), f"ROE 应当是小数口径，收到 {roe[:3]}"
    yields = [v for v in out["dividend_yield"].to_list() if v is not None]
    assert all(0.0 <= v <= 0.2 for v in yields), f"股息率应当是小数口径，收到 {yields[:3]}"
    assert len(out["published_date"].drop_nulls()) > 0, "披露日整列空着等于 PIT 闸门永久关闭"


@pytest.mark.live
def test_live_financials_on_a_negative_equity_stock_keep_the_ratios_unknown():
    """`002450` 是特意挑的：净资产为负（实测每股净资产 −3.52），东财于是**不给 ROE**，
    而分红送配对它回「查无记录」。这一只票走的正是市赚率最容易被误用的那条路——
    缺 ROE 必须是 null（PR 拒绝出数），不能是 0（PR 会把它读成「赚得刚好够本」）。
    """
    real = AkshareAdapter()
    out = real.financials("A", "002450", date(2023, 1, 1), date(2099, 12, 31))
    assert out.height > 0, "取到的是有期次的报表，只是 ROE 那一格源给不出"
    assert out["roe"].drop_nulls().to_list() == []
    assert out["dividend_yield"].drop_nulls().to_list() == []
    assert len(out["published_date"].drop_nulls()) == out.height


@pytest.mark.live
def test_live_valuation_has_a_real_pe_and_no_dividend_yield():
    real = AkshareAdapter()
    out = real.valuation("A", "601088", date(2024, 1, 1), date(2099, 12, 31))
    assert out.height > 300, "东财估值表给的是自上市以来的按日序列"
    assert len(out["pe_ttm"].drop_nulls()) > 0
    assert len(out["dividend_yield"].drop_nulls()) == 0, "这个源给不出按日股息率，别把它读成 0"


@pytest.mark.live
def test_live_flow_gives_main_net_in_and_leaves_north_bound_unknown():
    """A 股资金流端点已连通（2026-10-19 两次 `-m live` 连续通过，原来挂的 xfail 去掉）。

    留在这里的那三句断言是这份契约的全部形状知识：`main_net_in` 有数、`north_net` 全 NULL
    （这个端点不给北向，缺字段存 null 而不是 0，D58），而 U25 剩下的缺口是 HK/US 没有源。
    """
    real = AkshareAdapter()
    out = real.funding_flow("A", "601088", date(2026, 9, 1), date(2026, 10, 17))
    assert out.height > 0
    assert len(out["main_net_in"].drop_nulls()) > 0
    assert out["north_net"].to_list() == [None] * out.height


