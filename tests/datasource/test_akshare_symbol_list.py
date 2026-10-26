"""A 股代码表 = ak.stock_info_a_code_name()，HK = ak.stock_hk_spot_em() 的 代码/名称 列。

注入写法与 `test_akshare_listed_date.py` 一路：网络那一跳换成假 fetch，
这里钉的是「出口只留 (code, name) 两列、代码进规范形态、别的市场是一句答复不是故障」。
"""

from __future__ import annotations

import polars as pl
import pytest

from sams.core.errors import EmptyResponse
from sams.datasource.akshare_cn import AkshareAdapter
from sams.datasource.akshare_hk import AkshareHkAdapter


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
