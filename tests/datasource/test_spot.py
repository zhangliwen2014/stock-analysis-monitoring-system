"""全市场快照：字段位置是接口的硬事实，位置错一位就是把别的数写进这一列。

A 股那行 88 格是按四只票（茅台、平安银行、腾讯、苹果）交叉核过的；这里用同一形状的
录制行钉住 38 换手、39 PE(TTM)、43 振幅、44 流通市值、45 总市值、46 PB、49 量比、
52 PE(动)、35 复合字段第三段的成交额。

港股行的位置与 A 股不同，而且**同样是第 39 格，港股是 PE(动)**：拿 A 股那张表去读
港股，会把另一个倍数写进 PE(TTM)。这里的 HK 录制行来自 2026-10-30 的腾讯行情，并与
东财 F10 的 PE_TTM/PB/总市值逐只对过（00700 12.80 vs 12.86、2.67 vs 2.63、34716 亿
vs 34334 亿；00386 的 44/45 分别是流通与总市值）。换手、量比、PE(动) 在港股行里
没有已核对的格子，一律 None——不拿相邻位置的数顶上。
"""

from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

import pytest

from sams.core.errors import EmptyResponse, FieldDrift
from sams.datasource.spot import SPOT_BATCH, parse_spot, spot_symbol
from sams.datasource.tencent import QUOTE_URL, TencentAdapter, quote_get

ZONE = ZoneInfo("Asia/Shanghai")
HK_ZONE = ZoneInfo("Asia/Hong_Kong")
US_ZONE = ZoneInfo("America/New_York")


def line(key: str, cells: list[str]) -> str:
    return f'v_{key}="{"~".join(cells)}";'


def cells(
    *,
    code: str = "600519",
    name: str = "贵州茅台",
    price: str = "1330.75",
    ts: str = "20261028161454",
    pct: str = "1.85",
    turnover: str = "0.31",
    pe_ttm: str = "20.43",
    amplitude: str = "2.39",
    float_cap: str = "16635.46",
    cap: str = "16635.46",
    pb: str = "6.62",
    volume_ratio: str = "0.91",
    pe_dyn: str = "18.68",
    amount: str = "5187324618",
) -> list[str]:
    row = [""] * 88
    row[0] = "1"
    row[1] = name
    row[2] = code
    row[3] = price
    row[30] = ts
    row[32] = pct
    row[35] = f"{price}/38805/{amount}"
    row[38] = turnover
    row[39] = pe_ttm
    row[43] = amplitude
    row[44] = float_cap
    row[45] = cap
    row[46] = pb
    row[49] = volume_ratio
    row[52] = pe_dyn
    return row


def hk_cells(
    *,
    code: str = "00386",
    name: str = "中国石油化工股份",
    price: str = "4.760",
    ts: str = "2026/10/30 09:51:16",
    pct: str = "3.93",
    amount: str = "391862352.055",
    float_cap: str = "1126.9692",
    cap: str = "5746.0092",
    pe_ttm: str = "12.16",
    pb: str = "0.59",
    amplitude: str = "3.17",
) -> list[str]:
    """港股录制行：44 流通市值、45 总市值、57 PE(TTM)、58 PB（与 A 股不同）。"""
    row = [""] * 78
    row[0] = "100"
    row[1] = name
    row[2] = code
    row[3] = price
    row[30] = ts
    row[32] = pct
    row[37] = amount
    row[43] = amplitude
    row[44] = float_cap
    row[45] = cap
    row[57] = pe_ttm
    row[58] = pb
    return row


def us_cells(
    *,
    code: str = "AAPL",
    name: str = "苹果",
    price: str = "348.91",
    ts: str = "2026-10-29 16:00:01",
    pct: str = "1.48",
    amount: str = "14878265494",
    amplitude: str = "1.20",
    float_cap: str = "50888.90339",
    cap: str = "50920.55344",
    pe_ttm: str = "40.01",
) -> list[str]:
    """美股录制行（2026-10-29）：39 PE(TTM)、37 成交额、43 振幅、44/45 流通/总市值。

    这一行与新浪 `gb_aapl` 逐项对过：成交额 14,878,265,494、总市值 5,093.61 亿、
    PE 42.04（新浪用 8.30 的 EPS）vs 40.01（腾讯用 8.72 的 EPS）——两个源各自的
    PE 与自己的 EPS 自洽，位置对得上。PB 在美股行里没有已核对的位置。
    """
    row = [""] * 73
    row[0] = "200"
    row[1] = name
    row[2] = f"{code}.OQ"
    row[3] = price
    row[30] = ts
    row[32] = pct
    row[37] = amount
    row[39] = pe_ttm
    row[43] = amplitude
    row[44] = float_cap
    row[45] = cap
    return row


def test_field_positions_are_mapped_to_the_right_columns():
    frame = parse_spot(
        line("sh600519", cells()), market="A", codes=["600519"], zone=ZONE
    )
    row = frame.to_dicts()[0]
    assert row["code"] == "600519" and row["name"] == "贵州茅台"
    assert row["quoted_on"] == date(2026, 10, 28)
    assert row["price"] == 1330.75
    assert row["pct_chg"] == 1.85
    assert row["turnover_rate"] == 0.31
    assert row["pe_ttm"] == 20.43
    assert row["pe_dyn"] == 18.68
    assert row["pb"] == 6.62
    assert row["market_cap"] == 16635.46
    assert row["float_cap"] == 16635.46
    assert row["volume_ratio"] == 0.91
    assert row["amplitude"] == 2.39
    # 成交额在 A 股第 35 格的第三段，单位是元
    assert row["amount"] == 5187324618.0
    assert row["is_st"] is False
    assert row["source"] == "tencent"


def test_st_is_read_from_the_name_not_a_flag_column():
    frame = parse_spot(
        line("sz000004", cells(code="000004", name="*ST国华")),
        market="A",
        codes=["000004"],
        zone=ZONE,
    )
    assert frame.to_dicts()[0]["is_st"] is True


def test_bj_codes_use_the_bj_prefix():
    """公共的 a_share_symbol 会把 920xxx 发成 sz（实测无行，静默缺 353 只）。"""
    assert spot_symbol("A", "920992") == "bj920992"
    assert spot_symbol("A", "600519") == "sh600519"
    assert spot_symbol("A", "000001") == "sz000001"
    assert spot_symbol("HK", "00700") == "hk00700"


def test_hk_positions_are_not_the_a_share_positions():
    """港股 57/58 才是 PE(TTM)/PB；A 股同一形状的 39/46 在这里是别的数。"""
    frame = parse_spot(
        line("hk00386", hk_cells()), market="HK", codes=["00386"], zone=HK_ZONE
    )
    row = frame.to_dicts()[0]
    assert row["code"] == "00386" and row["name"] == "中国石油化工股份"
    assert row["quoted_on"] == date(2026, 10, 30)
    assert row["price"] == 4.76
    assert row["pct_chg"] == 3.93
    # 成交额在港股是第 37 格的单个数（A 股藏在第 35 格的复合字段里）
    assert row["amount"] == pytest.approx(391862352.055)
    assert row["amplitude"] == 3.17
    # 44/45 在同一行里必须分开：这只票的流通市值只有总市值的五分之一
    assert row["float_cap"] == pytest.approx(1126.9692)
    assert row["market_cap"] == pytest.approx(5746.0092)
    assert row["pe_ttm"] == 12.16
    assert row["pb"] == 0.59
    # 港股行里没核对的格子不拿相邻位置顶上
    assert row["turnover_rate"] is None
    assert row["volume_ratio"] is None
    assert row["pe_dyn"] is None
    assert row["is_st"] is False
    assert row["source"] == "tencent"


def test_us_positions_map_price_pe_amount_and_caps_but_not_pb():
    """美股与 A/HK 又一套位置：39 是 PE(TTM)、37 是成交额；PB 没核对，留 None。"""
    frame = parse_spot(
        line("usAAPL", us_cells()),
        market="US",
        codes=["AAPL"],
        zone=US_ZONE,
    )
    row = frame.to_dicts()[0]
    assert row["code"] == "AAPL" and row["name"] == "苹果"
    assert row["quoted_on"] == date(2026, 10, 29)
    assert row["price"] == 348.91
    assert row["pct_chg"] == 1.48
    assert row["amount"] == pytest.approx(14878265494)
    assert row["amplitude"] == 1.20
    assert row["pe_ttm"] == 40.01
    assert row["float_cap"] == pytest.approx(50888.90339)
    assert row["market_cap"] == pytest.approx(50920.55344)
    # 这三格在美股行里没有已核对的位置；PB 同理（D226）
    assert row["pb"] is None
    assert row["turnover_rate"] is None
    assert row["volume_ratio"] is None
    assert row["pe_dyn"] is None
    assert row["is_st"] is False
    assert row["source"] == "tencent"


def test_us_class_shares_keep_the_dot_in_the_quote_symbol():
    """`BRK.B` 与 `BRK.A` 是两类股：腾讯的 `usBRK.B` 有行，`usBRK-B` 没有。"""
    assert spot_symbol("US", "AAPL") == "usAAPL"
    assert spot_symbol("US", "BRK.B") == "usBRK.B"
    frame = parse_spot(
        line("usBRK.B", us_cells(code="BRK.B", name="伯克希尔B", price="541.96")),
        market="US",
        codes=["BRK.B"],
        zone=US_ZONE,
    )
    assert frame.to_dicts()[0]["code"] == "BRK.B"


def test_a_short_us_row_is_field_drift_not_a_row_of_nulls():
    with pytest.raises(FieldDrift, match="字段位置"):
        parse_spot(
            line("usAAPL", us_cells()[:40]),
            market="US",
            codes=["AAPL"],
            zone=US_ZONE,
        )


def test_a_short_hk_row_is_field_drift_not_a_row_of_nulls():
    with pytest.raises(FieldDrift, match="字段位置"):
        parse_spot(
            line("hk00386", hk_cells()[:50]),
            market="HK",
            codes=["00386"],
            zone=HK_ZONE,
        )


def test_a_short_row_is_field_drift_not_a_row_of_nulls():
    short = cells()[:40]
    with pytest.raises(FieldDrift, match="字段位置"):
        parse_spot(line("sh600519", short), market="A", codes=["600519"], zone=ZONE)


def test_a_response_without_any_match_is_empty_not_a_frame_of_zeros():
    with pytest.raises(EmptyResponse):
        parse_spot(
            line("sh600000", cells(code="600000", name="浦发银行")),
            market="A",
            codes=["600519"],
            zone=ZONE,
        )
    with pytest.raises(EmptyResponse):
        parse_spot("", market="A", codes=["600519"], zone=ZONE)


def test_adapter_slices_the_code_list_into_batches():
    """批大小是接口的硬边界：实测 880 个代码可、1000 个回 414。"""
    requested: list[list[str]] = []

    def fetch(url, params, *, timeout):
        requested.append(params["q"].split(","))
        code = params["q"].split(",")[0][2:]
        return line(params["q"].split(",")[0], cells(code=code, name=f"票{code}"))

    adapter = TencentAdapter(quote_fetch=fetch)
    codes = [f"{600000 + i}" for i in range(SPOT_BATCH + 3)]
    frame = adapter.market_snapshot("A", codes)
    assert [len(chunk) for chunk in requested] == [SPOT_BATCH, 3]
    # 替身每片只答第一只：两片各 1 行，合起来 2 行——这是「两片都解析并拼接」的证据
    assert frame.height == 2


def test_adapter_asks_us_with_the_bare_us_prefix():
    seen: list[str] = []

    def fetch(url, params, *, timeout):
        seen.extend(params["q"].split(","))
        return line("usAAPL", us_cells())

    adapter = TencentAdapter(quote_fetch=fetch)
    frame = adapter.market_snapshot("US", ["AAPL"])
    assert seen == ["usAAPL"]
    assert frame.to_dicts()[0]["market"] == "US"


def test_adapter_asks_hk_with_the_hk_prefix():
    seen: list[str] = []

    def fetch(url, params, *, timeout):
        seen.extend(params["q"].split(","))
        return line("hk00386", hk_cells())

    adapter = TencentAdapter(quote_fetch=fetch)
    frame = adapter.market_snapshot("HK", ["386"])
    assert seen == ["hk00386"]
    assert frame.to_dicts()[0]["market"] == "HK"


def test_the_snapshot_url_keeps_commas_literal(monkeypatch):
    """实测 880 只（URL 7941）可、1000 只（9021）回 414；`params=` 会把逗号编成 `%2C`，
    800 只因此涨到 8000 多——分批做了半天，最后还是撞在同一堵墙上（D186）。"""
    seen = {}

    class Response:
        status_code = 200
        content = b""

        def raise_for_status(self):
            pass

    def fake_get(url, **_kwargs):
        seen["url"] = url
        return Response()

    monkeypatch.setattr("requests.get", fake_get)
    query = ",".join(["sh600000"] * SPOT_BATCH)
    quote_get(QUOTE_URL, {"q": query})
    assert "%2C" not in seen["url"]
    assert len(seen["url"]) < 8192
    assert seen["url"].endswith("?q=" + query)


@pytest.mark.live
def test_live_snapshot_answers_real_rows_with_the_key_columns():
    """真接口冒烟：A 股 5 只必须都答得上，且 PE/PB/市值不是整列空。"""
    adapter = TencentAdapter()
    frame = adapter.market_snapshot("A", ["600519", "000001", "920992", "300750", "601088"])
    assert frame.height == 5
    assert frame.filter(frame["price"].is_not_null()).height == 5
    assert frame.filter(frame["pb"].is_not_null()).height >= 4


@pytest.mark.live
def test_live_snapshot_with_a_full_batch_fits_one_request():
    """880 只是实测的 URL 上限：整批必须过，否则 800 的分批白做。"""
    adapter = TencentAdapter()
    frame = adapter.market_snapshot("A", [f"{600000 + i}" for i in range(880)])
    assert frame.height > 500
