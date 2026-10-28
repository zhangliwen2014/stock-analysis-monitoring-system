"""全市场快照：字段位置是接口的硬事实，位置错一位就是把别的数写进这一列。

腾讯那行 88 格是按四只票（茅台、平安银行、腾讯、苹果）交叉核过的；这里用同一形状的
录制行钉住 38 换手、39 PE(TTM)、43 振幅、44 流通市值、45 总市值、46 PB、49 量比、
52 PE(动)、35 复合字段第三段的成交额。港美股行不测：那一侧不注册（D188）。
"""

from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

import pytest

from sams.core.errors import DatasourceError, EmptyResponse, FieldDrift
from sams.datasource.spot import SPOT_BATCH, parse_spot, spot_symbol
from sams.datasource.tencent import QUOTE_URL, TencentAdapter, quote_get

ZONE = ZoneInfo("Asia/Shanghai")


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


def test_adapter_refuses_hk_and_us():
    adapter = TencentAdapter(quote_fetch=lambda *a, **k: "")
    for market in ("HK", "US"):
        with pytest.raises(DatasourceError, match="只供"):
            adapter.market_snapshot(market, ["00700"])


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
