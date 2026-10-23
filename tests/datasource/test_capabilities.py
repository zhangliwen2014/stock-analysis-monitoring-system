"""「这个源能不能取这类数据」只有一份判据（M2-C 开工前收的那笔旧债）。

收敛前有三处各写一遍：`registry.CAPABILITIES`（键是方法名）、`sources._capabilities`
（键是 `bars`/`actions` 这种别名）、`services.sources` 里 `"bars" in …` 那句。
加一种数据要同时改三处，而漏改的那处不报错——它只是把没有这个能力的源算成有，
于是管理后台允许摘掉最后一个能拉价格的源，健康页显示一切正常（D38 说的正是这个后果）。
"""

from __future__ import annotations

import pytest

from sams.core.errors import DatasourceError
from sams.datasource.capabilities import CAPABILITIES, capability, provided_by
from sams.datasource.sources import build, catalog, known


def test_the_kind_name_is_the_adapter_method_name():
    """种类名 = 方法名 = `fetch_<方法名>`：没有第二套词，也就没有「两处叫法漂移」这件事。

    `listed_date` 与 `fx_rate` 是两个不走 `_walk` 的种类（一个问主档的一格，一个没有市场
    维度），但它们同样登记在这张表里：谁能填上市日期、谁能给汇率，判据仍然只有这一份。
    """
    assert set(CAPABILITIES) == {
        "daily_bars",
        "corp_actions",
        "financials",
        "valuation",
        "funding_flow",
        "listed_date",
        "fx_rate",
        "quote",
    }


def test_an_unknown_kind_says_which_ones_exist():
    """写错一个方法名要说清有哪些种类，而不是 KeyError 让人去翻字典。"""
    with pytest.raises(DatasourceError, match="不认识的数据种类") as exc:
        capability("financial")
    assert "daily_bars" in str(exc.value) and "corp_actions" in str(exc.value)


def test_the_registry_and_the_catalog_answer_from_the_same_probe():
    """路由跳过谁、目录说谁能干什么，必须是同一次判定。"""
    for kind, entry in catalog().by_kind().items():
        adapter = build(kind)
        assert provided_by(adapter) == entry.capabilities, kind
        for method in CAPABILITIES:
            assert capability(method).probe(adapter) == (method in entry.capabilities), (
                f"{kind} 在 {method} 上两处答案不同"
            )


def test_the_lacking_reason_is_per_kind_and_states_the_capability():
    """`skipped` 里那句「为什么这个源没被调用」要能被打印，所以它跟着种类走。"""
    assert CAPABILITIES["daily_bars"].lacking == "不提供日线"
    assert CAPABILITIES["corp_actions"].lacking == "不提供公司行为"
    assert known()["akshare_hk"].capabilities == ("corp_actions",)
    entries = catalog().by_kind()
    assert entries["tencent"].capabilities == ("daily_bars", "quote")
