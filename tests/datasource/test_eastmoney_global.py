"""东财港美股：分钟 K、HK 指数日线、HK 上市日期、HK/US 基本面、快照（#387 / D324）。

夹具是 2026-11-05 到 11-06 的实测读数：港股分钟 K 与美股分钟 K 的时间列都是**北京时间**，
接口给的 `clock` 是这一根的**结束时刻**；美股的 01:50（北京）要落成前一天的 13:50（纽约），
否则一根盘中 K 会跨到第二天、图上多出一整段假夜盘。
"""

from __future__ import annotations

from datetime import date

import pytest

from sams.core.errors import EmptyResponse, FieldDrift
from sams.datasource.eastmoney_global import parse_em_minute

D0 = date(2026, 11, 5)
D1 = date(2026, 11, 6)

#: 00700 实测 klt=5 最后两根（北京时间，列序：时刻/开/收/高/低/量/额）
HK_KLINE = {
    "rc": 0,
    "data": {
        "code": "00700",
        "market": 116,
        "name": "腾讯控股",
        "klines": [
            "2026-11-05 15:55,430.600,430.600,431.200,430.600,1249400,538321840.000",
            "2026-11-05 16:00,430.600,430.400,430.800,430.000,2651200,1141120608.000",
        ],
    },
}
#: AAPL 实测 klt=5 最后两根：北京 01:50/01:55 是纽约前一天 12:50/12:55（EST）
US_KLINE = {
    "rc": 0,
    "data": {
        "code": "AAPL",
        "market": 105,
        "name": "苹果",
        "klines": [
            "2026-11-06 01:50,344.260,345.070,345.230,343.990,426094,146922523.000",
            "2026-11-06 01:55,345.090,345.373,345.404,344.800,223872,77344727.000",
        ],
    },
}


def test_hk_minute_bars_keep_source_clock_and_column_order():
    out = parse_em_minute(HK_KLINE, market="HK", code="700", start=D0, end=D0)

    assert out.columns == [
        "market",
        "code",
        "date",
        "clock",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "source",
    ]
    assert out["code"].to_list() == ["00700", "00700"]
    assert out["clock"].to_list() == ["15:55", "16:00"]
    first = out.row(0, named=True)
    assert first["date"] == D0
    assert (first["open"], first["close"], first["high"], first["low"]) == (
        430.6,
        430.6,
        431.2,
        430.6,
    )
    assert (first["volume"], first["amount"]) == (1249400.0, 538321840.0)
    assert first["source"] == "eastmoney_global"


def test_us_minute_bars_are_converted_to_the_market_zone():
    """北京 01:50 是纽约前一天 12:50：日期跟着交易所走，不跟源走。"""
    out = parse_em_minute(US_KLINE, market="US", code="AAPL", start=D0, end=D0)

    assert out["date"].to_list() == [D0, D0]
    assert out["clock"].to_list() == ["12:50", "12:55"]
    assert out["close"].to_list() == [345.07, 345.373]


def test_minute_window_filters_bars_and_empty_answer_is_empty_response():
    with pytest.raises(EmptyResponse):
        parse_em_minute(HK_KLINE, market="HK", code="00700", start=D1, end=D1)


def test_null_data_is_an_empty_answer_but_missing_data_is_field_drift():
    with pytest.raises(EmptyResponse):
        parse_em_minute({"data": None}, market="HK", code="00700", start=D0, end=D0)
    with pytest.raises(FieldDrift):
        parse_em_minute({"rc": 0}, market="HK", code="00700", start=D0, end=D0)


# ---------- 适配器：市场意愿、secid 形态与窗口 ----------


class FakeKline:
    def __init__(self, payloads: dict[str, dict] | None = None) -> None:
        self.payloads = payloads or {}
        self.calls: list[tuple[str, int, date, date]] = []

    def __call__(self, secid: str, klt: int, start: date, end: date) -> dict:
        self.calls.append((secid, klt, start, end))
        return self.payloads.get(secid, {"data": None})


def test_us_class_share_uses_the_underscore_secid_and_resolution_is_cached():
    from sams.datasource.eastmoney_global import EastmoneyGlobalAdapter, em_us_symbol

    assert em_us_symbol("BRK.B") == "BRK_B"
    assert em_us_symbol("BRK.B.N") == "BRK_B"
    assert em_us_symbol("AAPL.OQ") == "AAPL"

    kline = FakeKline({"106.BRK_B": US_KLINE})
    resolved: list[str] = []

    def resolve(symbol: str) -> str | None:
        resolved.append(symbol)
        return "106.BRK_B"

    adapter = EastmoneyGlobalAdapter(kline_fetch=kline, us_secid_fetch=resolve)
    first = adapter.minute_bars("US", "BRK.B", D0, D0, scale=5, count=240)
    second = adapter.minute_bars("US", "BRK.B", D0, D0, scale=5, count=240)

    assert kline.calls[0][0] == "106.BRK_B"
    assert kline.calls[0][1] == 5
    assert resolved == ["BRK_B"], "同一次进程里同一只票只探一次 secid"
    assert first.height == second.height == 2


def test_us_minute_without_a_matching_secid_is_an_empty_answer():
    from sams.datasource.eastmoney_global import EastmoneyGlobalAdapter

    adapter = EastmoneyGlobalAdapter(
        kline_fetch=FakeKline(), us_secid_fetch=lambda symbol: None
    )
    with pytest.raises(EmptyResponse):
        adapter.minute_bars("US", "ZZZZ", D0, D0, scale=5, count=240)


def test_hk_minute_uses_the_116_prefix_and_rejects_unknown_scales():
    from sams.datasource.eastmoney_global import EastmoneyGlobalAdapter

    kline = FakeKline({"116.00700": HK_KLINE})
    adapter = EastmoneyGlobalAdapter(kline_fetch=kline)

    out = adapter.minute_bars("HK", "700", D0, D0, scale=5, count=240)
    assert kline.calls == [("116.00700", 5, D0, D0)]
    assert out["clock"].to_list() == ["15:55", "16:00"]

    with pytest.raises(ValueError):
        adapter.minute_bars("HK", "00700", D0, D0, scale=240, count=240)


def test_us_minute_asks_the_beijing_dates_around_the_new_york_day():
    """纽约 D0 的盘在北京 D0 21:30 到 D1 04:00：只问 [D0,D0] 实测回 0 根。

    请求窗口按北京时间放宽一天，出帧前再按换算后的纽约日期裁回调用方的窗口。
    """
    from sams.datasource.eastmoney_global import EastmoneyGlobalAdapter

    kline = FakeKline({"105.AAPL": US_KLINE})
    adapter = EastmoneyGlobalAdapter(kline_fetch=kline, us_secid_fetch=lambda s: "105.AAPL")

    out = adapter.minute_bars("US", "AAPL", D0, D0, scale=5, count=240)

    assert kline.calls == [("105.AAPL", 5, date(2026, 11, 4), date(2026, 11, 6))]
    assert out["date"].to_list() == [D0, D0], "出帧仍只留纽约的 D0"


# ---------- 按日估值（D331/D332） ----------


#: push2 实测读数（2026-11-06，fltt=2&invt=2）。f86 是秒级时间戳：
#: AAPL 1793912400 = 11-05 16:00 纽约（北京 11-06 05:00）；00700 1793952485 = 11-06 16:08 香港
US_QUOTE = {
    "rc": 0,
    "data": {
        "f57": "AAPL",
        "f86": 1793912400,
        "f162": "-",
        "f164": 37.84,
        "f167": 44.62,
        "f116": 5001541078940.0,
    },
}
HK_QUOTE = {
    "rc": 0,
    "data": {
        "f57": "00700",
        "f86": 1793952485,
        "f162": "-",
        "f164": 14.30,
        "f167": 2.92,
        "f116": 3817092299214.2,
    },
}
#: SPY 实测：ETF 没有 PE/PB，三格都是 "-"（不是 0）
SPY_QUOTE = {
    "rc": 0,
    "data": {"f57": "SPY", "f86": 1793912400, "f116": "-", "f164": "-", "f167": "-"},
}
#: 不存在的代码：rc=100、data 为 null
UNKNOWN_QUOTE = {"rc": 100, "rt": 1, "data": None}


class FakeQuote:
    def __init__(self, payloads: dict[str, dict] | None = None) -> None:
        self.payloads = payloads or {}
        self.calls: list[str] = []

    def __call__(self, secid: str) -> dict:
        self.calls.append(secid)
        return self.payloads.get(secid, {"data": None})


def test_hk_valuation_snapshot_maps_pe_pb_and_native_market_cap():
    from sams.core.fundamentals import VALUATION_COLUMNS
    from sams.datasource.eastmoney_global import parse_em_valuation

    out = parse_em_valuation(HK_QUOTE, market="HK", code="700", start=D1, end=D1)

    assert out.columns == list(VALUATION_COLUMNS)
    row = out.row(0, named=True)
    assert row["code"] == "00700"
    assert row["date"] == D1
    assert (row["pe_ttm"], row["pb"]) == (14.30, 2.92)
    assert row["market_cap"] == 3817092299214.2, "市值不换算：caliber 声明金额是本币原值"
    assert row["dividend_yield"] is None, "这一路没有股息率，不拿别的数替代"
    assert row["source"] == "eastmoney_global"


def test_us_valuation_snapshot_dates_the_row_in_new_york():
    """快照时间是纽约 11-05 16:00：行属于纽约的 D0，不属于北京的 D1。"""
    from sams.datasource.eastmoney_global import parse_em_valuation

    out = parse_em_valuation(US_QUOTE, market="US", code="AAPL", start=D0, end=D0)

    assert out.height == 1
    assert out["date"].to_list() == [D0]
    assert out.row(0, named=True)["market_cap"] == 5001541078940.0

    other = parse_em_valuation(US_QUOTE, market="US", code="AAPL", start=D1, end=D1)
    assert other.is_empty(), "日期在窗口外是 0 行，不是故障"


def test_valuation_snapshot_keeps_negative_pe_and_dash_cells_are_null():
    """负 PE 是一个真数（D59 靠它分叉）；`-` 是「不知道」，不是 0。"""
    from sams.datasource.eastmoney_global import parse_em_valuation

    rivn = {
        "data": {
            "f57": "RIVN",
            "f86": 1793912400,
            "f164": -6.52,
            "f167": 3.41,
            "f116": 19400000000.0,
        }
    }
    out = parse_em_valuation(rivn, market="US", code="RIVN", start=D0, end=D0)
    assert out.row(0, named=True)["pe_ttm"] == -6.52

    with pytest.raises(EmptyResponse):
        # 一个 ETF 的三个数都是 "-"：空白页不是「它没有估值」，不能写成 0 行发确认
        parse_em_valuation(SPY_QUOTE, market="US", code="SPY", start=D0, end=D0)


def test_valuation_snapshot_null_data_is_a_no_record_answer_but_no_clock_is_drift():
    from sams.datasource.eastmoney_global import parse_em_valuation

    with pytest.raises(EmptyResponse):
        parse_em_valuation(UNKNOWN_QUOTE, market="US", code="ZZZZQQ", start=D0, end=D0)

    no_clock = {"data": {"f57": "AAPL", "f164": 1.0, "f167": 1.0, "f116": 1.0}}
    with pytest.raises(FieldDrift):
        parse_em_valuation(no_clock, market="US", code="AAPL", start=D0, end=D0)


def test_the_adapter_valuation_uses_the_secid_and_caches_us_resolution():
    from sams.datasource.eastmoney_global import EastmoneyGlobalAdapter

    quote = FakeQuote({"116.00700": HK_QUOTE, "105.AAPL": US_QUOTE})
    resolved: list[str] = []

    def resolve(symbol: str) -> str | None:
        resolved.append(symbol)
        return "105.AAPL"

    adapter = EastmoneyGlobalAdapter(quote_fetch=quote, us_secid_fetch=resolve)
    hk = adapter.valuation("HK", "700", D1, D1)
    first = adapter.valuation("US", "AAPL", D0, D0)
    second = adapter.valuation("US", "AAPL", D0, D0)

    assert quote.calls[0] == "116.00700"
    assert hk.height == first.height == second.height == 1
    assert resolved == ["AAPL"], "同一只票的 secid 只探一次"


def test_a_snapshot_source_refuses_a_window_wider_than_a_week():
    """快照只有一天：长窗口说「答不了」，chain 才会继续问能给序列的源（D332）。

    不设这道闸，HK 一次三年回补会被「今天这一行」答掉，baidu 的逐日序列根本不会被问到。
    """
    from sams.datasource.eastmoney_global import EastmoneyGlobalAdapter

    quote = FakeQuote({"116.00700": HK_QUOTE})
    adapter = EastmoneyGlobalAdapter(quote_fetch=quote)

    with pytest.raises(EmptyResponse):
        adapter.valuation("HK", "700", date(2023, 1, 1), D1)
    assert quote.calls == [], "答不了的窗口不该先花一次请求"

    week = adapter.valuation("HK", "700", date(2026, 10, 31), D1)
    assert week.height == 1, "周末与公假后的补数窗口照常答"


# ---------- HK 指数日线 ----------

#: 恒指实测 klt=101 最后三根（列序：日期/开/收/高/低/量/额；第三格是 latest 即收盘）
HSI_KLINE = {
    "rc": 0,
    "data": {
        "code": "HSI",
        "name": None,
        "klines": [
            "2026-11-03,23469.64,23525.97,23549.31,23347.03,13583137792,236404375552.00",
            "2026-11-04,23451.79,23919.50,23919.50,23385.29,15802796288,236896239616.00",
            "2026-11-05,24316.94,24796.98,24871.92,24281.77,17585055232,356666703872.00",
        ],
    },
}


def test_hk_index_daily_reads_latest_as_close_and_keeps_the_source_code():
    from sams.datasource.base import BAR_COLUMNS
    from sams.datasource.eastmoney_global import EastmoneyGlobalAdapter, parse_em_hk_index

    out = parse_em_hk_index(HSI_KLINE, code="hkHSI", start=D0, end=D0)

    assert out.columns == BAR_COLUMNS
    assert out["code"].to_list() == ["hkHSI"]
    row = out.row(0, named=True)
    assert row["date"] == D0
    assert (row["open"], row["close"], row["high"], row["low"]) == (
        24316.94,
        24796.98,
        24871.92,
        24281.77,
    )
    assert row["volume"] == 17585055232.0
    assert row["source"] == "eastmoney_global"

    kline = FakeKline({"100.HSI": HSI_KLINE, "124.HSTECH": HSI_KLINE})
    adapter = EastmoneyGlobalAdapter(kline_fetch=kline)
    assert adapter.index_daily("HK", "hkHSI", D0, D0).height == 1
    assert kline.calls == [("100.HSI", 101, D0, D0)]
    # 恒生科技在 124，不在 100：secid 表按指数逐只写死
    adapter.index_daily("HK", "hkHSTECH", D0, D0)
    assert kline.calls[-1][0] == "124.HSTECH"


def test_hk_index_daily_refuses_us_and_unknown_hk_codes_without_raising():
    from sams.datasource.eastmoney_global import EastmoneyGlobalAdapter

    adapter = EastmoneyGlobalAdapter(kline_fetch=FakeKline())
    with pytest.raises(EmptyResponse):
        adapter.index_daily("HK", "hkHSI2", D0, D0)
    with pytest.raises(EmptyResponse):
        adapter.index_daily("US", "usINX", D0, D0)


# ---------- HK 上市日期 ----------

#: 00700 公司概况实测。LISTING_DATE 是「上市」，FOUND_DATE 是「成立」，不能混用
HK_PROFILE = {
    "result": {
        "pages": 1,
        "data": [
            {
                "SECUCODE": "00700.HK",
                "SECURITY_CODE": "00700",
                "LISTING_DATE": "2004-06-16 00:00:00",
                "FOUND_DATE": "1999-11-23",
            }
        ],
    }
}


class FakeGet:
    def __init__(self, payloads: dict[str, object] | None = None) -> None:
        self.payloads = payloads or {}
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, params: dict[str, str]) -> object:
        self.calls.append((url, dict(params)))
        report = params.get("reportName", "")
        return self.payloads.get(report, {"result": None})


def test_hk_listed_date_reads_listing_not_founding_date():
    from sams.datasource.eastmoney_global import EastmoneyGlobalAdapter, parse_hk_listed_date

    assert parse_hk_listed_date(HK_PROFILE) == date(2004, 6, 16)

    get = FakeGet({"RPT_HKF10_INFO_ORGPROFILE": HK_PROFILE})
    adapter = EastmoneyGlobalAdapter(get=get)
    assert adapter.listed_date("HK", "700") == date(2004, 6, 16)
    url, params = get.calls[0]
    assert params["filter"] == '(SECUCODE="00700.HK")'
    assert url.endswith("/api/data/v1/get")


def test_hk_listed_date_null_is_a_no_record_answer_but_a_missing_key_is_drift():
    from sams.datasource.eastmoney_global import (
        EastmoneyGlobalAdapter,
        parse_hk_listed_date,
    )

    assert parse_hk_listed_date({"result": None}) is None
    assert parse_hk_listed_date({"result": {"data": []}}) is None
    with pytest.raises(FieldDrift):
        parse_hk_listed_date({"rc": 0})
    with pytest.raises(FieldDrift):
        parse_hk_listed_date({"result": {"data": [{"SECURITY_CODE": "00700"}]}})

    # 美股这一路没有上市日期：一句「源没有」比一次假故障更贴近事实
    assert EastmoneyGlobalAdapter(get=FakeGet()).listed_date("US", "AAPL") is None


# ---------- HK 基本面 ----------

#: 00700 的 F10 主要指标全史实测前两行（带同比与报告期标签；披露日源里没有）
HK_FIN = {
    "result": {
        "pages": 1,
        "data": [
            {
                "SECUCODE": "00700.HK",
                "SECURITY_CODE": "00700",
                "REPORT_DATE": "2026-06-30 00:00:00",
                "REPORT_TYPE": "2026年中报",
                "OPERATE_INCOME": 401243000000,
                "OPERATE_INCOME_YOY": 10.0725325491,
                "HOLDER_PROFIT": 114115000000,
                "HOLDER_PROFIT_YOY": 10.3103944939,
                "ROE_AVG": 9.966353785254,
                "ROA": 5.436144417609,
                "GROSS_PROFIT_RATIO": 57.246606171323,
                "NET_PROFIT_RATIO": 29.2513514254,
                "DEBT_ASSET_RATIO": 43.3862926358,
                "BASIC_EPS": 12.639,
                "BPS": 124.919978879778,
                "PER_NETCASH_OPERATE": 16.9442389791,
                "CURRENT_RATIO": 1.286999908823,
            },
            {
                "SECUCODE": "00700.HK",
                "SECURITY_CODE": "00700",
                "REPORT_DATE": "2026-03-31 00:00:00",
                "REPORT_TYPE": "2026年一季报",
                "OPERATE_INCOME": 196458000000,
                "OPERATE_INCOME_YOY": 9.1299952228,
                "HOLDER_PROFIT": 58093000000,
                "HOLDER_PROFIT_YOY": 21.4801028837,
                "ROE_AVG": 5.091848379615,
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


def test_hk_financials_reuse_the_f10_parser_and_keep_the_report_type():
    from sams.datasource.eastmoney_global import EastmoneyGlobalAdapter

    get = FakeGet({"RPT_HKF10_FN_MAININDICATOR": HK_FIN})
    adapter = EastmoneyGlobalAdapter(get=get)
    out = adapter.financials("HK", "700", date(2026, 1, 1), date(2026, 12, 31))

    assert out.height == 2
    assert out["report_type"].to_list() == ["一季报", "中报"]
    mid = out.filter(out["report_date"] == date(2026, 6, 30)).row(0, named=True)
    assert mid["roe"] == pytest.approx(0.09966353785254)
    assert mid["revenue"] == 401243000000.0
    assert mid["published_date"] is None, "源不给披露日，不拿报告期加天数去猜（D61）"
    assert mid["source"] == "eastmoney_global"

    url, params = get.calls[0]
    assert params["reportName"] == "RPT_HKF10_FN_MAININDICATOR"
    assert params["filter"] == '(SECUCODE="00700.HK")'


def test_hk_financials_window_and_empty_answer():
    from sams.datasource.eastmoney_global import EastmoneyGlobalAdapter

    adapter = EastmoneyGlobalAdapter(get=FakeGet({"RPT_HKF10_FN_MAININDICATOR": HK_FIN}))
    only_mid = adapter.financials("HK", "00700", date(2026, 4, 1), date(2026, 12, 31))
    assert only_mid["report_date"].to_list() == [date(2026, 6, 30)]

    empty = EastmoneyGlobalAdapter(get=FakeGet()).financials(
        "HK", "00700", date(2026, 1, 1), date(2026, 12, 31)
    )
    assert empty.is_empty()
    assert "report_date" in empty.columns


# ---------- US 基本面（与 eastmoney_us_f10 同源，D322 第 2 档） ----------

US_PROFILE = {
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

US_ANNUAL = {
    "SECUCODE": "AAPL.O",
    "SECURITY_CODE": "AAPL",
    "DATE_TYPE": "年报",
    "DATE_TYPE_CODE": "001",
    "REPORT_DATE": "2025-09-27 00:00:00",
    "ROE_AVG": 171.4224497448,
    "OPERATE_INCOME_YOY": 6.4255117828,
    "PARENT_HOLDER_NETPROFIT_YOY": 19.4951779466,
    "OPERATE_INCOME": 416161000000,
    "PARENT_HOLDER_NETPROFIT": 112010000000,
    "BASIC_EPS": 7.49,
}


def test_us_financials_identify_the_report_kind_and_stamp_the_new_source():
    from sams.datasource.eastmoney_global import EastmoneyGlobalAdapter

    get = FakeGet(
        {
            "RPT_USF10_DATA_MAININDICATOR": US_PROFILE,
            "RPT_USF10_FN_GMAININDICATOR": {"result": {"data": [US_ANNUAL]}},
        }
    )
    out = EastmoneyGlobalAdapter(get=get).financials(
        "US", "AAPL", date(2025, 1, 1), date(2025, 12, 31)
    )

    row = out.row(0, named=True)
    assert row["report_date"] == date(2025, 9, 27), "9 月财年就是 9 月（D228）"
    assert row["report_type"] == "年报"
    assert row["roe"] == pytest.approx(1.714224497448)
    assert row["revenue"] == pytest.approx(416161000000)
    assert row["published_date"] is None
    assert row["source"] == "eastmoney_global", "备份源自己的名字，不冒用同源的另一个源"
    assert get.calls[0][1]["filter"] == '(SECURITY_CODE="AAPL")'
    assert get.calls[1][1]["reportName"] == "RPT_USF10_FN_GMAININDICATOR"


def test_us_financials_of_an_unknown_ticker_are_zero_rows_not_a_failure():
    from sams.datasource.eastmoney_global import EastmoneyGlobalAdapter

    empty = EastmoneyGlobalAdapter(get=FakeGet()).financials(
        "US", "ZZZZ", date(2025, 1, 1), date(2025, 12, 31)
    )
    assert empty.is_empty()
    assert "report_date" in empty.columns


# ---------- 全市场快照 ----------

#: clist 实测两行（2026-11-05 收盘后；列序按 f2/f3/f5/f6/f7/f8/f9/f10/f12/f13/f14/...）
HK_SPOT_ROWS = [
    {
        "f2": 430.4,
        "f3": 7.17,
        "f5": 56903938,
        "f6": 24168809728.0,
        "f7": 5.23,
        "f8": 0.63,
        "f9": 14.66,
        "f10": 2.72,
        "f12": "00700",
        "f13": 128,
        "f14": "腾讯控股",
        "f20": 3913474334402,
        "f21": 3913474334402,
        "f23": 2.99,
        "f115": 14.66,
        "f124": 1793866095,
    },
    {
        "f2": 80.3,
        "f3": 6.78,
        "f5": 90039092,
        "f6": 7272612864.0,
        "f7": 7.18,
        "f8": 1.61,
        "f9": -11.62,
        "f10": 3.14,
        "f12": "03690",
        "f13": 128,
        "f14": "美团-W",
        "f20": 495893594408,
        "f21": 449387609633,
        "f23": 2.55,
        "f115": -11.62,
        "f124": 1793866096,
    },
]
US_SPOT_ROWS = [
    {
        "f2": 173.755,
        "f3": -1.93,
        "f5": 1126037,
        "f6": 196063565.0,
        "f7": 3.07,
        "f8": 0.4,
        "f9": 34.02,
        "f10": 1.04,
        "f12": "A",
        "f13": 106,
        "f14": "安捷伦",
        "f20": 48986548027,
        "f21": 48986548027,
        "f23": 6.65,
        "f115": 34.02,
        "f124": 1793901383,
    }
]
#: 牛熊证那一行：涨跌幅 −90%，PE/PB/市值在源里是 "-"
WARRANT_ROW = {
    "f2": 0.01,
    "f3": -90.1,
    "f5": 11048460032,
    "f6": 385606528.0,
    "f7": 43.56,
    "f8": "-",
    "f9": "-",
    "f10": 460352.5,
    "f12": "64043",
    "f13": 116,
    "f14": "恒指摩通九三熊G",
    "f20": "-",
    "f21": "-",
    "f23": "-",
    "f115": "-",
    "f124": 1793862401,
}


def test_hk_snapshot_maps_the_measured_columns_to_the_standard_frame():
    from sams.datasource.eastmoney_global import parse_em_spot

    out = parse_em_spot(HK_SPOT_ROWS, market="HK", codes=["700", "03690"])

    assert out["code"].to_list() == ["00700", "03690"]
    row = out.row(0, named=True)
    assert row["name"] == "腾讯控股"
    assert row["quoted_on"] == D0, "f124 是 UTC 秒级时点，日期按交易所时区落"
    assert (row["price"], row["pct_chg"]) == (430.4, 7.17)
    assert row["amount"] == 24168809728.0
    assert row["turnover_rate"] == 0.63
    assert row["volume_ratio"] == 2.72
    assert row["amplitude"] == 5.23
    assert row["pe_ttm"] == 14.66
    assert row["pe_dyn"] == 14.66
    assert row["pb"] == 2.99
    assert row["market_cap"] == pytest.approx(3913474334402.0)
    assert row["float_cap"] == pytest.approx(3913474334402.0)
    assert row["is_st"] is False
    assert row["source"] == "eastmoney_global"


def test_us_snapshot_maps_the_underscore_class_share_back_to_the_system_code():
    from sams.datasource.eastmoney_global import parse_em_spot

    rows = [dict(US_SPOT_ROWS[0], f12="BRK_B", f14="伯克希尔-B")]
    out = parse_em_spot(rows, market="US", codes=["BRK.B"])

    (row,) = out.iter_rows(named=True)
    assert row["code"] == "BRK.B"
    assert row["quoted_on"] == D0, "北京 11-06 01:56 是纽约 11-05 12:56"
    assert row["pe_ttm"] == 34.02
    assert row["pe_dyn"] == 34.02
    assert row["market_cap"] == pytest.approx(48986548027.0)


def test_snapshot_dash_cells_are_unknown_and_unrequested_rows_are_skipped():
    from sams.datasource.eastmoney_global import parse_em_spot

    out = parse_em_spot([*HK_SPOT_ROWS, WARRANT_ROW], market="HK", codes=["00700"])
    assert out.height == 1, "没请求的代码不进帧（全表 4745 行也不是这个函数的事）"

    warrant = parse_em_spot([WARRANT_ROW], market="HK", codes=["64043"]).row(0, named=True)
    assert warrant["price"] == 0.01
    assert warrant["pe_ttm"] is None and warrant["pb"] is None
    assert warrant["market_cap"] is None and warrant["float_cap"] is None
    assert warrant["turnover_rate"] is None

    with pytest.raises(EmptyResponse):
        parse_em_spot(HK_SPOT_ROWS, market="HK", codes=["ZZZZ"])
    with pytest.raises(EmptyResponse):
        parse_em_spot([], market="US", codes=["AAPL"])


def test_the_adapter_snapshot_fetches_the_whole_market_once_per_call():
    from sams.datasource.eastmoney_global import EastmoneyGlobalAdapter

    calls: list[str] = []

    def spot(market: str) -> list[dict]:
        calls.append(market)
        return HK_SPOT_ROWS if market == "HK" else []

    adapter = EastmoneyGlobalAdapter(spot_fetch=spot)
    out = adapter.market_snapshot("HK", ["700", "03690", "00700"])
    assert out["code"].to_list() == ["00700", "03690"], "请求里的重复代码只问一次"
    assert calls == ["HK"]

    with pytest.raises(EmptyResponse):
        adapter.market_snapshot("HK", [])
    with pytest.raises(EmptyResponse):
        # 空表是一句答复，不是故障
        adapter.market_snapshot("US", ["AAPL"])


def test_the_global_source_provides_six_capabilities():
    from sams.datasource.capabilities import provided_by
    from sams.datasource.eastmoney_global import EastmoneyGlobalAdapter

    assert provided_by(EastmoneyGlobalAdapter) == (
        "financials",
        "valuation",
        "listed_date",
        "index_daily",
        "minute_bars",
        "market_snapshot",
    )


def test_snapshot_fetch_walks_pages_until_total(monkeypatch):
    from sams.datasource import eastmoney_global

    pages = {
        1: {"data": {"total": 2, "diff": HK_SPOT_ROWS[:1]}},
        2: {"data": {"total": 2, "diff": HK_SPOT_ROWS[1:]}},
    }
    pns: list[str] = []
    params_seen: list[dict] = []

    def get(url: str, params: dict[str, str]) -> dict:
        pns.append(params["pn"])
        params_seen.append(dict(params))
        return pages[int(params["pn"])]

    monkeypatch.setattr(eastmoney_global.time, "sleep", lambda _s: None)
    rows = eastmoney_global.fetch_snapshot("HK", get=get)

    assert [row["f12"] for row in rows] == ["00700", "03690"]
    assert pns == ["1", "2"], "总行数到了就不再多问一页"
    assert params_seen[0]["pz"] == "100", "一页硬上限 100：传大也不回更多"
    assert params_seen[0]["fs"] == "m:128 t:3,m:128 t:4,m:128 t:1,m:128 t:2"


def test_snapshot_fetch_stops_on_an_empty_page_and_rejects_other_markets(monkeypatch):
    from sams.datasource import eastmoney_global

    pages = {
        1: {"data": {"total": None, "diff": HK_SPOT_ROWS[:1]}},
        2: {"data": {"total": None, "diff": []}},
    }
    monkeypatch.setattr(eastmoney_global.time, "sleep", lambda _s: None)

    def get(url: str, params: dict[str, str]) -> dict:
        return pages[int(params["pn"])]

    assert len(eastmoney_global.fetch_snapshot("HK", get=get)) == 1

    with pytest.raises(ValueError):
        eastmoney_global.fetch_snapshot("A", get=get)
    with pytest.raises(FieldDrift):
        eastmoney_global.fetch_snapshot("HK", get=lambda url, params: {"data": {"total": 1}})
