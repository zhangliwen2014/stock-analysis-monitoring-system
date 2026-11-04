"""美股主要财务指标：东财 F10 三张报表 + 按财年认年报（D228）。

这一层要钉住四件事：百分数只除一次、披露日留空、年报按报告类型认（苹果 9 月财年）、
一般企业/银行/保险三张报表的列各按各的映射走。任何一件做错，屏幕上都是一个
看起来正常、实际差一百倍或整列缺失的数。
"""

from __future__ import annotations

from datetime import date

import pytest

from sams.core.errors import DatasourceError, FieldDrift, TransientError
from sams.datasource.capabilities import provided_by
from sams.datasource.eastmoney_us_f10 import (
    EastmoneyUSF10Adapter,
    parse_us_financials,
)

ANNUAL = {
    "SECUCODE": "AAPL.O",
    "SECURITY_CODE": "AAPL",
    "DATE_TYPE": "年报",
    "DATE_TYPE_CODE": "001",
    "REPORT_DATE": "2025-09-27 00:00:00",
    "ROE_AVG": 171.4224497448,
    "OPERATE_INCOME_YOY": 6.4255117828,
    "PARENT_HOLDER_NETPROFIT_YOY": 19.4951779466,
    "ROA": 30.9325468331,
    "GROSS_PROFIT_RATIO": 46.9051641072,
    "NET_PROFIT_RATIO": 26.9150641218,
    "DEBT_ASSET_RATIO": 79.4753382827,
    "OPERATE_INCOME": 416161000000,
    "PARENT_HOLDER_NETPROFIT": 112010000000,
    "BASIC_EPS": 7.49,
    "CURRENT_RATIO": 0.893292922219,
}

QUARTER = {
    **ANNUAL,
    "DATE_TYPE": "单季报",
    "DATE_TYPE_CODE": "006",
    "REPORT_DATE": "2026-06-27 00:00:00",
    "ROE_AVG": 27.8387559518,
}

GENERAL_PAYLOAD = {"result": {"data": [QUARTER, ANNUAL]}}

PROFILE = {
    "result": {
        "data": [
            {
                "SECUCODE": "AAPL.O",
                "SECURITY_CODE": "AAPL",
                "SECURITY_NAME_ABBR": "苹果",
                "ORG_TYPE": "一般企业",
            }
        ]
    }
}

BANK_ANNUAL = {
    "SECUCODE": "JPM.N",
    "SECURITY_CODE": "JPM",
    "DATE_TYPE": "年报",
    "DATE_TYPE_CODE": "001",
    "REPORT_DATE": "2025-12-31 00:00:00",
    "ROE": 17.0,
    "TOTAL_INCOME_YOY": 8.0,
    "PARENT_HOLDER_NETPROFIT_YOY": 6.0,
    "ROA": 1.3,
    "TOTAL_INCOME": 180000000000,
    "PARENT_HOLDER_NETPROFIT": 60000000000,
    "BASIC_EPS_CS": 20.5,
}

INSURANCE_ANNUAL = {
    **BANK_ANNUAL,
    "SECUCODE": "PGR.N",
    "SECURITY_CODE": "PGR",
    "DEBT_RATIO": 73.0,
}


def test_the_us_annual_report_becomes_decimal_ratios_without_a_publication_date():
    out = parse_us_financials(
        GENERAL_PAYLOAD,
        market="US",
        code="AAPL",
        source="eastmoney_us_f10",
        org_type="一般企业",
    )
    (row,) = out.iter_rows(named=True)
    assert row["market"] == "US" and row["code"] == "AAPL"
    assert row["report_date"] == date(2025, 9, 27), "9 月财年就是 9 月，不改写成 12-31"
    assert row["report_type"] == "年报"
    # 源给百分数，÷100 只发生在采集归一这一步（D58）
    assert row["roe"] == pytest.approx(1.714224497448)
    assert row["roa"] == pytest.approx(0.309325468331)
    assert row["gross_margin"] == pytest.approx(0.469051641072)
    assert row["debt_ratio"] == pytest.approx(0.794753382827)
    # NOTICE_DATE 在老数据上是东财的入库日，不能拿来当披露日（D228）
    assert row["published_date"] is None
    assert row["dividend_yield"] is None and row["market_cap"] is None


def test_only_the_annual_rows_are_taken_from_the_report():
    """同一张表里年报与单季报混排；季度 ROE 是三个月的数，不能冒充年报（D62）。"""
    out = parse_us_financials(
        GENERAL_PAYLOAD,
        market="US",
        code="AAPL",
        source="eastmoney_us_f10",
        org_type="一般企业",
    )
    assert out["report_date"].to_list() == [date(2025, 9, 27)]


def test_the_wind_display_columns_are_mapped_and_the_missing_ones_stay_null():
    out = parse_us_financials(
        GENERAL_PAYLOAD,
        market="US",
        code="AAPL",
        source="eastmoney_us_f10",
        org_type="一般企业",
    )
    row = out.row(0, named=True)
    assert row["revenue"] == pytest.approx(416161000000)
    assert row["net_profit"] == pytest.approx(112010000000)
    assert row["eps"] == pytest.approx(7.49)
    assert row["current_ratio"] == pytest.approx(0.893292922219)
    # 源的主指标没有这些列：留 NULL，不拿别的表硬凑
    assert row["bps"] is None and row["roic"] is None and row["quick_ratio"] is None


def test_banks_read_revenue_from_total_income():
    out = parse_us_financials(
        {"result": {"data": [BANK_ANNUAL]}},
        market="US",
        code="JPM",
        source="eastmoney_us_f10",
        org_type="银行",
    )
    row = out.row(0, named=True)
    assert row["roe"] == pytest.approx(0.17)
    assert row["revenue"] == pytest.approx(180000000000)
    assert row["revenue_yoy"] == pytest.approx(0.08)
    assert row["net_profit"] == pytest.approx(60000000000)
    # 银行没有毛利/净利率/流动比率：这三格是 NULL，不是 0
    assert row["gross_margin"] is None
    assert row["net_margin"] is None
    assert row["current_ratio"] is None


def test_insurers_keep_the_debt_ratio_the_banks_do_not_give():
    out = parse_us_financials(
        {"result": {"data": [INSURANCE_ANNUAL]}},
        market="US",
        code="PGR",
        source="eastmoney_us_f10",
        org_type="保险",
    )
    row = out.row(0, named=True)
    assert row["debt_ratio"] == pytest.approx(0.73)
    assert row["revenue"] == pytest.approx(180000000000)


def test_a_null_result_is_an_empty_frame():
    out = parse_us_financials(
        {"result": None, "success": False, "message": "返回数据为空"},
        market="US",
        code="AAPL",
        source="eastmoney_us_f10",
        org_type="一般企业",
    )
    assert out.is_empty()


def test_a_missing_annual_column_is_field_drift():
    broken = {
        "result": {
            "data": [
                {
                    "SECURITY_CODE": "AAPL",
                    "DATE_TYPE": "年报",
                    "DATE_TYPE_CODE": "001",
                    "REPORT_DATE": "2025-09-27 00:00:00",
                }
            ]
        }
    }
    with pytest.raises(FieldDrift, match="ROE_AVG"):
        parse_us_financials(
            broken,
            market="US",
            code="AAPL",
            source="eastmoney_us_f10",
            org_type="一般企业",
        )


def test_the_window_filters_by_fiscal_period_end():
    out = parse_us_financials(
        GENERAL_PAYLOAD,
        market="US",
        code="AAPL",
        source="eastmoney_us_f10",
        org_type="一般企业",
        start=date(2025, 1, 1),
        end=date(2025, 12, 31),
    )
    assert out["report_date"].to_list() == [date(2025, 9, 27)]


def test_the_adapter_identifies_the_security_then_asks_the_general_report():
    calls: list[tuple[str, dict]] = []

    def get(url, params):
        calls.append((url, dict(params)))
        if len(calls) == 1:
            return PROFILE
        return GENERAL_PAYLOAD

    EastmoneyUSF10Adapter(get=get).financials(
        "US", "AAPL", date(2025, 1, 1), date(2025, 12, 31)
    )
    assert len(calls) == 2
    first, second = calls[0][1], calls[1][1]
    assert first["reportName"] == "RPT_USF10_DATA_MAININDICATOR"
    assert first["filter"] == '(SECURITY_CODE="AAPL")'
    assert second["reportName"] == "RPT_USF10_FN_GMAININDICATOR"
    assert second["filter"] == '(SECUCODE="AAPL.O")(DATE_TYPE_CODE in ("001"))'
    assert "datacenter.eastmoney.com" in calls[0][0]


def test_the_adapter_routes_banks_to_the_bank_report():
    calls: list[dict] = []

    def get(url, params):
        calls.append(dict(params))
        if len(calls) == 1:
            return {
                "result": {
                    "data": [{"SECUCODE": "JPM.N", "SECURITY_CODE": "JPM", "ORG_TYPE": "银行"}]
                }
            }
        return {"result": {"data": [BANK_ANNUAL]}}

    out = EastmoneyUSF10Adapter(get=get).financials(
        "US", "JPM", date(2025, 1, 1), date(2025, 12, 31)
    )
    assert calls[1]["reportName"] == "RPT_USF10_FN_BMAININDICATOR"
    assert calls[1]["columns"] == "ALL"
    assert out.row(0, named=True)["revenue"] == pytest.approx(180000000000)


def test_a_ticker_the_source_does_not_know_is_an_empty_answer_not_a_failure():
    calls: list[dict] = []

    def get(url, params):
        calls.append(dict(params))
        return {"result": None, "success": False, "message": "返回数据为空"}

    out = EastmoneyUSF10Adapter(get=get).financials(
        "US", "BRK.B", date(2025, 1, 1), date(2025, 12, 31)
    )
    assert out.is_empty()
    assert len(calls) == 1, "查无证券就不必再问报表"


def test_a_code_that_could_break_the_filter_is_refused():
    def get(url, params):  # pragma: no cover - 不该被调用
        raise AssertionError("非法代码不该发出去")

    with pytest.raises(DatasourceError, match="代码"):
        EastmoneyUSF10Adapter(get=get).financials(
            "US", 'AAPL") OR 1=1 --', date(2025, 1, 1), date(2025, 12, 31)
        )


def test_connection_errors_are_transient():
    def get(url, params):
        raise TimeoutError("timed out")

    with pytest.raises(TransientError):
        EastmoneyUSF10Adapter(get=get).financials(
            "US", "AAPL", date(2025, 1, 1), date(2025, 12, 31)
        )


def test_it_declares_only_the_financials_capability():
    assert provided_by(EastmoneyUSF10Adapter) == ("financials",)
    assert EastmoneyUSF10Adapter.markets == frozenset({"US"})
