"""A 股代码表 = ak.stock_info_a_code_name()，HK = ak.stock_hk_spot_em() 的 代码/名称 列。

注入写法与 `test_akshare_listed_date.py` 一路：网络那一跳换成假 fetch，
这里钉的是「出口只留 (code, name) 两列、代码进规范形态、别的市场是一句答复不是故障」。
"""

from __future__ import annotations

import polars as pl
import pytest

from sams.core.errors import EmptyResponse
from sams.datasource.akshare_cn import AkshareAdapter
from sams.datasource.akshare_hk import AkshareHkAdapter, akshare_hk_symbol_list


def test_a_symbol_list_injected():
    def fake():
        return pl.DataFrame({"code": ["000001", "600519 "], "name": ["平安银行", "贵州茅台"]})

    adapter = AkshareAdapter(list_fetch=fake)
    out = adapter.symbol_list("A")
    assert out.to_dicts() == [
        {"code": "000001", "name": "平安银行"},
        {"code": "600519", "name": "贵州茅台"},
    ]


def test_a_symbol_list_other_market_empty():
    """A 源没有港股那张表：EmptyResponse 是一句答复，降级链据此继续问下一个源。"""

    def fake():
        return pl.DataFrame({"code": ["000001"], "name": ["平安银行"]})

    adapter = AkshareAdapter(list_fetch=fake)
    with pytest.raises(EmptyResponse):
        adapter.symbol_list("HK")


def test_hk_symbol_list_injected():
    def fake():
        return pl.DataFrame({"代码": ["00700"], "名称": ["腾讯控股"]})

    adapter = AkshareHkAdapter(list_fetch=fake)
    assert adapter.symbol_list("HK").to_dicts() == [{"code": "00700", "name": "腾讯控股"}]


def test_hk_symbol_list_other_market_empty():
    def fake():
        return pl.DataFrame({"代码": ["00700"], "名称": ["腾讯控股"]})

    adapter = AkshareHkAdapter(list_fetch=fake)
    with pytest.raises(EmptyResponse):
        adapter.symbol_list("A")


def test_hk_symbol_list_falls_back_to_sina_when_em_fails():
    """东财港股快照连续 RemoteDisconnected：新浪那张表是同一件事的第二个源（D210）。"""

    def broken():
        raise RuntimeError("Remote end closed connection without response")

    def sina():
        return pl.DataFrame({"代码": ["00700", "01021"], "中文名称": ["腾讯控股", "中国银河"]})

    adapter = AkshareHkAdapter(
        list_fetch=lambda: akshare_hk_symbol_list(primary=broken, fallback=sina)
    )
    assert adapter.symbol_list("HK").to_dicts() == [
        {"code": "00700", "name": "腾讯控股"},
        {"code": "01021", "name": "中国银河"},
    ]


def test_hk_symbol_list_falls_back_when_em_answers_empty():
    def empty():
        return pl.DataFrame()

    def sina():
        return pl.DataFrame({"代码": ["00700"], "中文名称": ["腾讯控股"]})

    out = akshare_hk_symbol_list(primary=empty, fallback=sina)
    # 链出口统一成 名称：归一函数只认 code/名称，新浪的「中文名称」在这里就翻好
    assert out.to_dicts() == [{"代码": "00700", "名称": "腾讯控股"}]


def test_both_hk_symbol_list_sources_empty_is_empty_response():
    """「东财挂了」与「港股没有代码表」不是同一句话：两个都答空才是后者。"""

    def empty():
        return pl.DataFrame()

    adapter = AkshareHkAdapter(
        list_fetch=lambda: akshare_hk_symbol_list(primary=empty, fallback=empty)
    )
    with pytest.raises(EmptyResponse):
        adapter.symbol_list("HK")


def test_the_last_hk_symbol_list_error_is_the_one_reported():
    """两个源都抛错时报最后一个：运维要知道「新浪现在也不行」，而不是去修已经挂了的东财。"""

    def em_down():
        raise RuntimeError("eastmoney down")

    def sina_down():
        raise ConnectionError("sina down")

    with pytest.raises(ConnectionError, match="sina down"):
        akshare_hk_symbol_list(primary=em_down, fallback=sina_down)
