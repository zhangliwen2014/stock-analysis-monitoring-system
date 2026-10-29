from __future__ import annotations

from datetime import date

import pytest

from sams.core.errors import FieldDrift, TransientError
from sams.datasource.capabilities import provided_by
from sams.datasource.eastmoney_hk_f10 import (
    EastmoneyHKF10Adapter,
    parse_hk_financials,
)

PAYLOAD = {
    "result": {
        "pages": 20,
        "data": [
            {
                "SECUCODE": "00700.HK",
                "SECURITY_CODE": "00700",
                "REPORT_DATE": "2026-06-30 00:00:00",
                "REPORT_TYPE": "2026年中报",
                "ROE_AVG": 9.966353785254,
                "OPERATE_INCOME_YOY": 10.0725325491,
                "HOLDER_PROFIT_YOY": 10.3103944939,
            },
            {
                "SECUCODE": "00700.HK",
                "SECURITY_CODE": "00700",
                "REPORT_DATE": "2026-03-31 00:00:00",
                "REPORT_TYPE": "2026年一季报",
                "ROE_AVG": 5.091848379615,
                "OPERATE_INCOME_YOY": 9.1299952228,
                "HOLDER_PROFIT_YOY": 21.4801028837,
            },
        ],
    }
}


def test_hk_indicators_become_decimal_ratios_without_a_publication_date():
    out = parse_hk_financials(
        PAYLOAD, market="HK", code="700", source="eastmoney_hk_f10"
    )
    assert out.height == 2
    latest = out.sort("report_date", descending=True).row(0, named=True)
    assert latest["market"] == "HK" and latest["code"] == "00700"
    assert latest["report_date"] == date(2026, 6, 30)
    # 百分数 ÷100 只发生在采集归一这一步（D58）
    assert latest["roe"] == pytest.approx(0.09966353785254)
    assert latest["revenue_yoy"] == pytest.approx(0.100725325491)
    assert latest["profit_yoy"] == pytest.approx(0.103103944939)
    # 源不给披露日：拿报告期加天数去猜就是往回开时间机器（D61/D215）
    assert latest["published_date"] is None
    assert latest["dividend_yield"] is None
    assert latest["market_cap"] is None


def test_a_result_of_null_is_an_empty_frame():
    """`{"result": null, "message": "返回数据为空"}` 是源在答「没有」，不是改版。"""
    out = parse_hk_financials(
        {"result": None, "success": False}, market="HK", code="00700",
        source="eastmoney_hk_f10",
    )
    assert out.is_empty()


def test_a_missing_result_node_is_field_drift():
    with pytest.raises(FieldDrift, match="result"):
        parse_hk_financials({}, market="HK", code="00700", source="eastmoney_hk_f10")


def test_a_missing_indicator_column_is_field_drift():
    broken = {"result": {"data": [{"SECURITY_CODE": "00700", "REPORT_DATE": "2026-06-30"}]}}
    with pytest.raises(FieldDrift, match="ROE_AVG"):
        parse_hk_financials(broken, market="HK", code="00700", source="eastmoney_hk_f10")


def test_the_window_filters_by_report_period():
    out = parse_hk_financials(
        PAYLOAD,
        market="HK",
        code="00700",
        source="eastmoney_hk_f10",
        start=date(2026, 4, 1),
        end=date(2026, 12, 31),
    )
    assert out["report_date"].to_list() == [date(2026, 6, 30)]


def test_the_adapter_queries_the_hk_indicator_report():
    calls: list[tuple[str, dict]] = []

    def get(url, params):
        calls.append((url, dict(params)))
        return PAYLOAD

    adapter = EastmoneyHKF10Adapter(get=get)
    adapter.financials("HK", "00700", date(2026, 1, 1), date(2026, 12, 31))
    url, params = calls[-1]
    assert "RPT_HKF10_FN_MAININDICATOR" in params["reportName"]
    assert params["filter"] == '(SECUCODE="00700.HK")'
    assert "datacenter.eastmoney.com" in url


def test_connection_errors_are_transient():
    def get(url, params):
        raise TimeoutError("timed out")

    with pytest.raises(TransientError):
        EastmoneyHKF10Adapter(get=get).financials(
            "HK", "00700", date(2026, 1, 1), date(2026, 12, 31)
        )


def test_it_declares_the_financials_capability():
    assert "financials" in provided_by(EastmoneyHKF10Adapter)
