"""代码表归一：只留 (code, name) 两列，代码进规范形态，空答复就是 EmptyResponse。"""

import polars as pl
import pytest

from sams.core.errors import EmptyResponse
from sams.datasource.symbol_list import has_symbol_list, normalize_symbol_list


def test_normalize_two_column_shapes():
    en = normalize_symbol_list("A", pl.DataFrame({"code": ["600519"], "name": [" 贵州茅台 "]}))
    assert en.to_dicts() == [{"code": "600519", "name": "贵州茅台"}]
    cn = normalize_symbol_list("HK", pl.DataFrame({"代码": ["00700"], "名称": ["腾讯控股"]}))
    assert cn.to_dicts() == [{"code": "00700", "name": "腾讯控股"}]


def test_normalize_canonicalizes_and_dedupes():
    raw = pl.DataFrame({"代码": ["700", "hk0700", ""], "名称": ["腾讯", "腾讯", "空"]})
    out = normalize_symbol_list("HK", raw)
    assert out.height == 1  # 别名归到 00700 只留一条，空码丢弃
    assert out["code"][0] == "00700"


def test_empty_is_empty_response():
    with pytest.raises(EmptyResponse):
        normalize_symbol_list("A", pl.DataFrame({"code": [], "name": []}))


def test_missing_columns_is_empty_response():
    with pytest.raises(EmptyResponse):
        normalize_symbol_list("A", pl.DataFrame({"id": ["1"]}))


def test_has_symbol_list():
    class With:
        def symbol_list(self, market): ...

    class Without:
        pass

    assert has_symbol_list(With())
    assert not has_symbol_list(Without())
