"""可插拔注册表：加一个源到底要不要改别的文件。

这个文件测的是「承诺」本身：实现自己声明能服务哪些市场、名字必须先在表里存在才能落库、
参数只能传实现真的接受的。三条都关系到管理后台能不能被信任——
界面上写的 `kind` 和 options 最终会变成构造函数的参数。
"""

from __future__ import annotations

import pytest

from sams.core.errors import DatasourceError
from sams.datasource import sources
from sams.datasource.sources import catalog, discover, kinds, known, markets_of, source

#: 本包里的适配器模块：每个都必须往注册表里放至少一个实现
ADAPTER_MODULES = {
    "akshare_cn",
    "akshare_hk",
    "akshare_ths",
    "eastmoney",
    "eastmoney_f10",
    "eastmoney_hk_f10",
    "sina",
    "sina_quote",
    "tencent",
    "yfinance_us",
}


@pytest.fixture(autouse=True)
def _restore_registry():
    """用例可以注册临时实现，但不能把它留给下一个用例。"""
    snapshot = dict(sources._REGISTRY)
    yield
    sources._REGISTRY.clear()
    sources._REGISTRY.update(snapshot)


class Dummy:
    name = "dummy"
    markets = frozenset({"A", "HK"})

    def __init__(self, *, timeout: int = 15) -> None:
        self.timeout = timeout

    def daily_bars(self, market, code, start, end):  # pragma: no cover - 从未被调用
        raise AssertionError("注册表不该调用适配器")


def register_dummy(**kwargs) -> str:
    kind = "dummy_for_test"
    source(kind=kind, **kwargs)(Dummy)
    return kind


def test_discovered_modules_all_register_an_implementation():
    """新文件放进包里却忘了 `@source`，症状是「这个源看起来不存在」——
    排查的人会一路去查网络。让它在测试里当场红。
    """
    found = set(discover())
    assert found == ADAPTER_MODULES, "包目录里出现了没登记的模块，或 _NOT_ADAPTERS 需要更新"
    modules = {entry.module for entry in catalog().entries}
    assert found <= modules, f"这些模块没注册任何实现: {sorted(found - modules)}"


def test_capabilities_come_from_the_methods_not_from_a_hand_kept_list():
    """D38 的「这个源能不能干这件事」由方法是否存在推出来。

    再造一份「源 → 能力」的表一定会漂移，而漂移的后果不是报错：健康页会把只有
    公司行为的源算成日线的备源，管理后台也就允许摘掉最后一个能拉价格的源。
    """
    entries = catalog().by_kind()
    assert entries["akshare_hk"].capabilities == ("corp_actions", "symbol_list")
    assert entries["akshare_ths"].capabilities == ("corp_actions",)
    assert entries["tencent"].capabilities == (
        "daily_bars",
        "quote",
        "index_daily",
        "minute_series",
        "minute_bars",
        "market_snapshot",
    )
    assert entries["akshare"].capabilities == (
        "daily_bars",
        "corp_actions",
        "financials",
        "valuation",
        "funding_flow",
        "listed_date",
        "symbol_list",
    ), "A 股主源现在是七种数据都能给：加一个方法就多一种能力，这里跟着动，别处不用改"


def test_registering_exposes_the_implementations_own_markets():
    kind = register_dummy(options=("timeout",))
    assert markets_of(kind) == frozenset({"A", "HK"})
    assert kind in kinds() and kind in known()


def test_build_passes_only_the_declared_options_to_the_constructor():
    kind = register_dummy(options=("timeout",))
    assert sources.build(kind, {"timeout": 3}).timeout == 3
    assert sources.build(kind).timeout == 15


def test_unknown_kind_is_refused_with_the_valid_list():
    with pytest.raises(DatasourceError, match="未注册"):
        sources.build("no-such-source")


def test_undeclared_option_is_refused_at_build():
    kind = register_dummy()
    # 管理后台里写 `base_url` 这种参数，必须在装配时炸，而不是静默忽略：
    # 静默忽略等于「我以为换了地址，其实一直在打原接口」
    with pytest.raises(DatasourceError, match="不接受参数"):
        sources.build(kind, {"base_url": "https://evil.example"})


def test_declaring_an_option_the_constructor_does_not_take_is_a_registration_error():
    with pytest.raises(DatasourceError, match="不接受参数"):
        register_dummy(options=("page_size",))


def test_a_class_that_does_not_declare_markets_cannot_be_registered():
    class NoMarkets:
        name = "no-markets"

    with pytest.raises(DatasourceError, match="未声明 markets"):
        source("no_markets")(NoMarkets)


def test_supporting_lists_only_what_can_serve_the_market():
    register_dummy(options=("timeout",))
    assert "tencent" in catalog().supporting("HK")
    assert "sina" not in catalog().supporting("HK")
    assert catalog().supporting("A") == tuple(sorted(catalog().supporting("A")))


def test_default_adapters_builds_every_registered_implementation():
    """采集路由的默认适配器集合来自注册表，不再是一份硬编码清单：
    清单会漏，漏掉的源在配置里看起来像「写错了名字」。
    """
    from sams.datasource.registry import default_adapters

    assert set(default_adapters()) == set(known())


def test_catalog_entries_carry_a_note_for_the_admin_surface():
    """加源界面要说得出「这个源是干嘛的」，否则运维只能靠猜名字。"""
    assert all(entry.note for entry in catalog().entries)
