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
                "OPERATE_INCOME": 401243000000,
                "HOLDER_PROFIT": 114115000000,
                "ROA": 5.436144417609,
                "GROSS_PROFIT_RATIO": 57.246606171323,
                "NET_PROFIT_RATIO": 29.2513514254,
                "DEBT_ASSET_RATIO": 43.3862926358,
                "BASIC_EPS": 12.639,
                "BPS": 124.919978879778,
                "PER_NETCASH_OPERATE": 16.9442389791,
                "CURRENT_RATIO": 1.286999908823,
                # 年度口径的 ROIC 与季度列混排会撒谎：这一列在源里有，但我们不取
                "ROIC_YEARLY": 20.5,
            },
            {
                "SECUCODE": "00700.HK",
                "SECURITY_CODE": "00700",
                "REPORT_DATE": "2026-03-31 00:00:00",
                "REPORT_TYPE": "2026年一季报",
                "ROE_AVG": 5.091848379615,
                "OPERATE_INCOME_YOY": 9.1299952228,
                "HOLDER_PROFIT_YOY": 21.4801028837,
                "OPERATE_INCOME": 196458000000,
                "HOLDER_PROFIT": 58093000000,
                "ROA": 2.7,
                "GROSS_PROFIT_RATIO": 55.1,
                "NET_PROFIT_RATIO": 29.57,
                "DEBT_ASSET_RATIO": 42.9,
                "BASIC_EPS": 6.3,
                "BPS": 118.2,
                "PER_NETCASH_OPERATE": 9.1,
                "CURRENT_RATIO": 1.25,
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


# ---------- 0028：Wind 式展示列（D217） ----------


def test_hk_indicators_map_the_wind_columns_and_normalise_the_report_type():
    out = parse_hk_financials(
        PAYLOAD, market="HK", code="00700", source="eastmoney_hk_f10"
    )
    latest = out.sort("report_date", descending=True).row(0, named=True)
    # 源给「2026年中报」：筛档用的是归一后的四个标签
    assert latest["report_type"] == "中报"
    assert latest["revenue"] == pytest.approx(401243000000)
    assert latest["net_profit"] == pytest.approx(114115000000)
    assert latest["eps"] == pytest.approx(12.639)
    assert latest["bps"] == pytest.approx(124.919978879778)
    assert latest["ocf_per_share"] == pytest.approx(16.9442389791)
    assert latest["gross_margin"] == pytest.approx(0.57246606171323)
    assert latest["net_margin"] == pytest.approx(0.292513514254)
    assert latest["roa"] == pytest.approx(0.05436144417609)
    assert latest["debt_ratio"] == pytest.approx(0.433862926358)
    assert latest["current_ratio"] == pytest.approx(1.286999908823)


def test_hk_columns_the_source_cannot_give_are_left_null():
    """速动/存货周转/总资产周转在源里是「天」不是「次」，ROIC 只有年度口径：
    这些列留 NULL，屏幕显示「—」，不拿倒数或年度数硬凑（D217）。"""
    out = parse_hk_financials(
        PAYLOAD, market="HK", code="00700", source="eastmoney_hk_f10"
    )
    latest = out.sort("report_date", descending=True).row(0, named=True)
    assert latest["roic"] is None
    assert latest["quick_ratio"] is None
    assert latest["inventory_turnover"] is None
    assert latest["total_asset_turnover"] is None


def test_an_unrecognised_report_type_stays_null_and_the_month_filter_can_still_work():
    payload = {
        "result": {
            "data": [
                {**PAYLOAD["result"]["data"][0], "REPORT_TYPE": "中期业绩"}
            ]
        }
    }
    out = parse_hk_financials(
        payload, market="HK", code="00700", source="eastmoney_hk_f10"
    )
    assert out.row(0, named=True)["report_type"] is None
