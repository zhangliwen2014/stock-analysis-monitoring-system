from __future__ import annotations

from datetime import date

import pytest

from sams.core.errors import FieldDrift, TransientError
from sams.datasource.capabilities import provided_by
from sams.datasource.eastmoney_f10 import (
    EastmoneyF10Adapter,
    f10_code,
    parse_announcements,
    parse_profile,
)

A_PROFILE = {
    "jbzl": [
        {
            "SECURITY_NAME_ABBR": "贵州茅台",
            "ORG_NAME": "贵州茅台酒股份有限公司",
            "ORG_NAME_EN": "Kweichow Moutai Co.,Ltd.",
            "EM2016": "食品饮料-饮料-白酒",
            "INDUSTRYCSRC1": "制造业-酒、饮料和精制茶制造业",
            "TRADE_MARKET": "上海证券交易所",
            "SECURITY_TYPE": "上交所主板A股",
            "CHAIRMAN": "陈华",
            "PRESIDENT": "王莉(代)",
            "SECRETARY": "余思明",
            "LEGAL_PERSON": "陈华",
            "ORG_TEL": "0851-22386002",
            "ORG_EMAIL": "mtdm@moutaichina.com",
            "ORG_WEB": "www.moutaichina.com",
            "ADDRESS": "贵州省仁怀市茅台镇",
            "REG_ADDRESS": "贵州省仁怀市茅台镇",
            "EMP_NUM": 34992,
            "ACCOUNTFIRM_NAME": "天健会计师事务所(特殊普通合伙)",
            "ORG_PROFILE": "  贵州茅台酒股份有限公司成立于1999年11月20日。",
        }
    ],
    "fxxg": [
        {
            "FOUND_DATE": "1999-11-20 00:00:00",
            "LISTING_DATE": "2001-08-27 00:00:00",
        }
    ],
}

HK_PROFILE = {
    "zqzl": {
        "zqdm": "00700.HK",
        "zqjc": "腾讯控股",
        "ssrq": "2004/6/16 0:00:00",
        "zqlx": "非H股",
        "jys": "香港交易所",
        "bk": "主板",
        "mgmz": "0.00002 HKD",
        "zxjydw": "100",
        "isin": "KYG875721634",
        "sfhgtbd": "是",
        "sfsgtbd": "是",
    },
    "gszl": {
        "gsmc": "腾讯控股有限公司",
        "ywmc": "Tencent Holdings Limited",
        "gsclrq": "1999-11-23",
        "bgdz": "香港湾仔皇后大道东1号太古广场三座29楼",
        "dsz": "马化腾",
        "gswz": "www.tencent.com",
        "gsms": "刘淑仪",
        "njr": "12-31",
        "email": "ir@tencent.com",
        "ygrs": "87,412",
        "lxdh": "+852 3148-5100",
        "hss": "罗兵咸永道会计师事务所",
        "gsjs": "腾讯是一家互联网科技公司。",
        "sshy": "软件服务",
    },
}

A_ANNOUNCEMENTS = {
    "data": {
        "list": [
            {
                "art_code": "AN202608141827994407",
                "codes": [{"stock_code": "600519"}],
                "columns": [{"column_name": "其他"}, {"column_name": "调研"}],
                "notice_date": "2026-08-15 00:00:00",
                "display_time": "2026-08-14 20:41:29:380",
                "title": "贵州茅台:关于召开2026年半年度业绩说明会的公告",
            }
        ]
    }
}

HK_ANNOUNCEMENTS = {
    "data": {
        "list": [
            {
                "art_code": "ANHK20260105001",
                "codes": [{"stock_code": "00700"}],
                "columns": [{"column_name": "其他"}],
                "notice_date": "2026-01-05 00:00:00",
                "title": "腾讯控股:公告",
            }
        ]
    }
}

CALLS: list[tuple[str, dict]] = []


def recorded(payload, *, raises=None):
    def get(url, params):
        CALLS.append((url, dict(params)))
        if raises is not None:
            raise raises
        return payload

    return get


@pytest.fixture(autouse=True)
def _clear_calls():
    CALLS.clear()


def test_a_profile_maps_the_f10_fields():
    out = parse_profile(A_PROFILE, market="A", code="600519", source="eastmoney_f10")
    assert out.height == 1
    row = out.row(0, named=True)
    assert row["market"] == "A" and row["code"] == "600519"
    assert row["name"] == "贵州茅台"
    assert row["full_name"] == "贵州茅台酒股份有限公司"
    assert row["industry"] == "食品饮料-饮料-白酒"
    assert row["industry_csrc"] == "制造业-酒、饮料和精制茶制造业"
    assert row["exchange"] == "上海证券交易所"
    assert row["board"] == "上交所主板A股"
    assert row["listed_on"] == date(2001, 8, 27)
    assert row["founded_on"] == date(1999, 11, 20)
    assert row["employees"] == 34992
    assert row["summary"] == "  贵州茅台酒股份有限公司成立于1999年11月20日。"
    assert row["isin"] is None  # A 股这一格源里没有，不拿空串冒充


def test_hk_profile_maps_isin_and_the_connect_flag():
    out = parse_profile(HK_PROFILE, market="HK", code="700", source="eastmoney_f10")
    row = out.row(0, named=True)
    assert row["market"] == "HK" and row["code"] == "00700"
    assert row["name"] == "腾讯控股"
    assert row["full_name"] == "腾讯控股有限公司"
    assert row["industry"] == "软件服务"
    assert row["listed_on"] == date(2004, 6, 16)
    assert row["founded_on"] == date(1999, 11, 23)
    assert row["isin"] == "KYG875721634"
    assert row["lot_size"] == 100
    assert row["hk_connect"] is True
    assert row["stock_type"] == "非H股"
    assert row["secretary"] == "刘淑仪"
    assert row["fiscal_year_end"] == "12-31"
    assert row["employees"] == 87412


def test_placeholder_dashes_are_not_kept_as_fields():
    """源用「--」表示没给（01021 的电话/邮箱），别把它当正文摆到屏幕上。"""
    payload = {
        "jbzl": [{**A_PROFILE["jbzl"][0], "ORG_TEL": "--", "ORG_EMAIL": "—"}],
        "fxxg": A_PROFILE["fxxg"],
    }
    row = parse_profile(
        payload, market="A", code="600519", source="eastmoney_f10"
    ).row(0, named=True)
    assert row["phone"] is None and row["email"] is None


def test_a_profile_without_the_basic_block_is_field_drift():
    with pytest.raises(FieldDrift, match="jbzl"):
        parse_profile({"fxxg": []}, market="A", code="600519", source="eastmoney_f10")


def test_an_empty_profile_block_is_an_empty_frame_not_a_failure():
    """源答「这只票没有概况记录」是一句答复：换下一个源，而不是判改版。"""
    out = parse_profile({"jbzl": [], "fxxg": []}, market="A", code="600519", source="eastmoney_f10")
    assert out.is_empty()
    assert "summary" in out.columns


def test_a_announcements_take_notice_date_and_build_the_detail_url():
    out = parse_announcements(
        A_ANNOUNCEMENTS, market="A", code="600519", source="eastmoney_f10"
    )
    row = out.row(0, named=True)
    assert row["published_on"] == date(2026, 8, 15)
    assert row["category"] == "其他、调研"
    assert row["art_code"] == "AN202608141827994407"
    assert row["url"] == (
        "https://data.eastmoney.com/notices/detail/600519/AN202608141827994407.html"
    )


def test_an_empty_announcement_list_is_an_empty_frame():
    out = parse_announcements(
        {"data": {"list": []}}, market="A", code="600519", source="eastmoney_f10"
    )
    assert out.is_empty()
    assert "published_on" in out.columns


def test_missing_data_node_is_field_drift():
    with pytest.raises(FieldDrift, match="data"):
        parse_announcements({}, market="A", code="600519", source="eastmoney_f10")


def test_an_announcement_echoing_another_code_is_field_drift():
    bad = {
        "data": {
            "list": [
                {
                    "art_code": "AN1",
                    "codes": [{"stock_code": "000001"}],
                    "columns": [],
                    "notice_date": "2026-08-15 00:00:00",
                    "title": "别的票",
                }
            ]
        }
    }
    with pytest.raises(FieldDrift, match="不一致"):
        parse_announcements(bad, market="A", code="600519", source="eastmoney_f10")


def test_the_adapter_asks_the_market_specific_endpoints():
    profile = EastmoneyF10Adapter(get=recorded(A_PROFILE))
    profile.company_profile("A", "600519", date(2026, 1, 1), date(2026, 1, 31))
    url, params = CALLS[-1]
    assert "PC_HSF10/CompanySurvey" in url
    assert params == {"code": "SH600519"}

    announcements = EastmoneyF10Adapter(get=recorded(HK_ANNOUNCEMENTS))
    frame = announcements.announcements("HK", "700", date(2026, 1, 1), date(2026, 1, 31))
    url, params = CALLS[-1]
    assert "np-anotice-stock" in url
    assert params["ann_type"] == "H"
    assert params["stock_list"] == "00700"
    assert params["begin_time"] == "2026-01-01"
    assert params["end_time"] == "2026-01-31"
    assert "/notices/detail/00700/" in frame.row(0, named=True)["url"]


def test_connection_errors_are_transient():
    adapter = EastmoneyF10Adapter(get=recorded(None, raises=TimeoutError("timed out")))
    with pytest.raises(TransientError):
        adapter.company_profile("A", "600519", date(2026, 1, 1), date(2026, 1, 31))


@pytest.mark.parametrize(
    "market, code, expected",
    [
        ("A", "600519", "SH600519"),
        ("A", "000001", "SZ000001"),
        ("A", "430047", "BJ430047"),
        ("HK", "700", "00700"),
    ],
)
def test_f10_code_shape(market, code, expected):
    assert f10_code(market, code) == expected


def test_the_new_capabilities_are_discoverable():
    offered = provided_by(EastmoneyF10Adapter)
    assert "company_profile" in offered and "announcements" in offered
