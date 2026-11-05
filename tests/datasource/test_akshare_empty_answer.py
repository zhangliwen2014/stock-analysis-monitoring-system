"""库里崩出来的「没有」要读成一句答复，不是一次故障（D327）。

11-05 实测：`stock_value_em` 对 `000996` / `999999` 回 `result: null`，akshare 自己在
`stock_value_em.py:37`（`data_json["result"]["data"]`）崩成
`TypeError: 'NoneType' object is not subscriptable`。同一接口对 600519/000001/300750
各回 2144/2144/2039 行——那句 `null` 是「这张表里没有它」，不是它挂了。

把它写成「源挂了」的代价不是文案难看：连败会把源推进冷却（D279），而 A 的估值链上只有
akshare 一个源，冷却之后那条链一个源都不剩——#365 记的正是这个形状。
"""

from __future__ import annotations

from datetime import date

import pytest

from sams.core.errors import DatasourceError, EmptyResponse
from sams.datasource.akshare_cn import AkshareAdapter

START, END = date(2024, 1, 1), date(2024, 6, 30)
#: akshare 对 `null` 结果的那一句崩溃原文（照原样钉住，别用更宽的词）
AKSHARE_NULL_CRASH = "'NoneType' object is not subscriptable"


def crash(message: str):
    def call(*_args, **_kwargs):
        raise TypeError(message)

    return call


@pytest.mark.parametrize(
    ("method", "over"),
    [
        ("valuation", {"valuation_fetch": crash(AKSHARE_NULL_CRASH)}),
        ("financials", {"financials_fetch": crash(AKSHARE_NULL_CRASH)}),
        ("funding_flow", {"flow_fetch": crash(AKSHARE_NULL_CRASH)}),
    ],
)
def test_a_null_result_from_the_library_is_an_answer_not_a_failure(method, over):
    with pytest.raises(EmptyResponse) as got:
        getattr(AkshareAdapter(**over), method)("A", "000996", START, END)
    assert got.value.source == "akshare"


def test_a_different_type_error_is_still_a_failure():
    """只认那一种形状。我们自己写挂的 `TypeError` 必须继续出声（D327 的边界）。"""
    adapter = AkshareAdapter(
        valuation_fetch=crash("unsupported operand type(s) for -: 'int' and 'str'")
    )
    with pytest.raises(DatasourceError) as got:
        adapter.valuation("A", "600519", START, END)
    assert not isinstance(got.value, EmptyResponse)
