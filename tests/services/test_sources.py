"""数据源管理的用例规则：能加什么、什么时候不许停、单独试一个源。

最要紧的一条是「不许把某个市场弄成零可用源」。它不是洁癖：
停掉唯一的源在**当下**没有任何症状——采集要到下一次才发现，
而那时候人已经离开终端了。宁可让这条命令当场失败。

注意这些用例看到的链来自**迁移种子**（HK 是 yfinance → tencent），
不是测试 Settings 里的 `sources`：库里一旦有记录，配置就只是兜底。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest
from support import sample_bars

from sams.core.errors import ValidationError
from sams.datasource.sina import SinaAdapter
from sams.services.sources import SourceAdminService
from sams.storage.repos.data_sources import DataSourceRepo

START, END = date(2024, 1, 2), date(2024, 1, 5)


@pytest.fixture
def repo(db) -> DataSourceRepo:
    return DataSourceRepo(db)


@pytest.fixture
def admin(repo, settings) -> SourceAdminService:
    return SourceAdminService(repo, settings)


@pytest.fixture
def narrow_admin(repo, settings) -> SourceAdminService:
    """配置里只留 A 的服务：用来测「实现支持、系统没配」那一档拒绝。

    三市场全配好的 settings 里这一档永远走不到（实现的覆盖是配置的子集），
    而真实部署恰恰可能只开一个市场。
    """
    narrowed = replace(settings, markets={"A": settings.markets["A"]})
    return SourceAdminService(repo, narrowed)


def make_hk_single_source(admin):
    """把 HK 收成只剩 yfinance：种子给了两个源，直接用默认状态测不出失明。"""
    admin.reorder("HK", ["yfinance"])


def test_seed_gives_hk_and_us_a_second_source(admin):
    """spec §18 U5 结案的那条：港美股不再是单源。"""
    assert len(admin.chains()["HK"]) > 1 and len(admin.chains()["US"]) > 1


def test_add_appends_at_the_tail_of_each_market(admin, repo):
    admin.add("tx2", "tencent", ["A", "HK"], note="第二实例")
    chains = admin.chains()
    assert chains["A"][-1] == "tx2" and chains["HK"][-1] == "tx2"
    assert repo.get("tx2").note == "第二实例"


def test_add_falls_back_to_the_implementations_own_note(admin):
    assert admin.add("tx3", "tencent", ["A"]).note


def test_add_can_be_registered_disabled(admin):
    row = admin.add("tx4", "tencent", ["A"], enabled=False)
    assert not row.enabled
    assert "tx4" not in admin.chains()["A"], "停用源不进链"
    assert [m for m, n, _ in admin.routes() if n == "tx4"] == ["A"], "路由保留，启用即回位"


def test_add_rejects_an_unregistered_kind(admin):
    with pytest.raises(ValidationError, match="未注册"):
        admin.add("nope", "not-a-real-source", ["A"])


def test_add_rejects_a_module_path_as_kind(admin):
    """这条是安全边界：接受 import 路径等于让「加数据源」变成任意代码执行。"""
    with pytest.raises(ValidationError, match="未注册"):
        admin.add("evil", "os.system", ["A"])


@pytest.mark.parametrize("markets,match", [(["JP"], "不支持市场"), ([], "至少要给一个市场")])
def test_add_rejects_markets_the_implementation_cannot_serve(admin, markets, match):
    with pytest.raises(ValidationError, match=match):
        admin.add("tx5", "tencent", markets)


def test_add_rejects_a_market_that_is_not_configured(narrow_admin):
    # 实现支持 US，但这份配置里没有 US：时区与币种都不知道，接进来也采不了
    with pytest.raises(ValidationError, match="没在配置文件的"):
        narrow_admin.add("sax", "tencent", ["A", "US"])


def test_add_rejects_options_the_implementation_does_not_take(admin):
    # 不挡的话，错误要等到下次装配构造适配器时才炸，而那时没人看着加源界面
    with pytest.raises(ValidationError, match="不接受参数"):
        admin.add("tx6", "tencent", ["A"], options={"base_url": "https://evil"})


def test_options_are_stored_verbatim(admin, repo):
    admin.add("tx7", "tencent", ["A"], options={"timeout": 7})
    assert repo.get("tx7").options == {"timeout": 7}


def test_disable_is_refused_when_it_would_blind_a_market(admin):
    make_hk_single_source(admin)
    with pytest.raises(ValidationError, match="完全取不到数"):
        admin.set_enabled("yfinance", False)


def test_disable_is_allowed_when_a_backup_remains(admin):
    admin.set_enabled("akshare", False)
    assert "akshare" not in admin.chains()["A"]
    assert admin.chains()["A"], "A 股链还有备源"


def test_remove_follows_the_same_rule_as_disable(admin):
    make_hk_single_source(admin)
    with pytest.raises(ValidationError, match="完全取不到数"):
        admin.remove("yfinance")


def test_unroute_is_refused_for_the_last_source(admin):
    admin.unroute("HK", "tencent")
    with pytest.raises(ValidationError, match="先加备源"):
        admin.unroute("HK", "yfinance")


def test_unroute_joins_the_market_the_way_the_routes_are_stored(admin):
    """`unroute a …` 不许报「市场 a 摘掉它就失明了」——A 还有四个源，且那句是劝你别敲。

    `route`/`reorder`/`probe` 先过 `_require_routable`（那里按大写比对能力），小写市场
    到不了比较；`unroute` 是唯一绕过它的一条，于是 `a` 一路走到 `_bars_sources_in`
    的 `m == market`，对上的是库里的大写行 ⇒ 一个都不剩，报错。键的比对两侧同形（D50）。
    """
    assert admin.unroute("a", "sina") is True
    assert "sina" not in admin.chains()["A"]


def test_unroute_names_the_canonical_market_when_the_chain_would_go_dark(admin):
    admin.unroute("hk", "tencent")
    with pytest.raises(ValidationError, match="市场 HK"):
        admin.unroute("Hk", "yfinance")


def test_an_actions_only_source_is_not_a_backup_for_bars(admin):
    """摘掉最后一个能取日线的源，链上还剩 akshare_hk——它一个价格都拉不到。

    「还剩几个源」不是判据，「还剩几个能取日线的源」才是（D38）：按前者放行，
    `sams health` 会说 HK 有两个源，而采集要到下一次才发现 HK 失明。
    """
    assert admin.unroute("HK", "tencent")
    with pytest.raises(ValidationError, match="先加备源"):
        admin.unroute("HK", "yfinance")


def test_disable_follows_the_same_capability_rule(admin):
    admin.unroute("HK", "tencent")
    with pytest.raises(ValidationError, match="完全取不到数"):
        admin.set_enabled("yfinance", False)
    assert admin.chains()["HK"] == ("yfinance", "akshare_hk"), "被拒绝的操作不许改库"


def test_a_second_source_unblocks_disabling_the_first(admin):
    admin.add("yf2", "yfinance", ["HK"])
    admin.set_enabled("yfinance", False)
    assert "yfinance" not in admin.chains()["HK"]
    assert set(admin.chains()["HK"]) >= {"tencent", "yf2"}


def test_route_refuses_a_source_that_cannot_serve_the_market(admin):
    with pytest.raises(ValidationError, match="不支持市场"):
        admin.route("HK", "akshare", 1)


def test_route_refuses_an_unknown_source(admin):
    with pytest.raises(ValidationError, match="先 add"):
        admin.route("A", "ghost", 1)


def test_route_refuses_an_unconfigured_market(narrow_admin, repo):
    with pytest.raises(ValidationError, match="没在配置里定义"):
        narrow_admin.route("US", "tencent", 1)


@pytest.mark.parametrize("names,match", [([], "为空"), (["ghost"], "不存在")])
def test_reorder_validates_the_whole_chain(admin, names, match):
    with pytest.raises(ValidationError, match=match):
        admin.reorder("A", names)


def test_reorder_rewrites_priority(admin):
    admin.reorder("A", ["tencent", "akshare", "eastmoney"])
    assert admin.chains()["A"] == ("tencent", "akshare", "eastmoney")
    assert [(m, n, p) for m, n, p in admin.routes() if m == "A"] == [
        ("A", "tencent", 1),
        ("A", "akshare", 2),
        ("A", "eastmoney", 3),
    ]


def test_update_can_retune_options_and_note(admin):
    admin.add("tx8", "tencent", ["A"], options={"timeout": 15}, note="默认")
    row = admin.update("tx8", options={"timeout": 3}, note="内网走短超时")
    assert row.options == {"timeout": 3} and row.note == "内网走短超时"


def test_update_leaves_untouched_fields_alone(admin):
    admin.add("tx9", "tencent", ["A"], options={"timeout": 9})
    assert admin.update("tx9", note="只改备注").options == {"timeout": 9}


def test_update_refuses_unknown_source_and_bad_options(admin):
    with pytest.raises(ValidationError, match="不存在"):
        admin.update("ghost", note="x")
    with pytest.raises(ValidationError, match="不接受参数"):
        admin.update("akshare", options={"timeout": 1})


def test_supported_market_candidates_come_from_the_registry(admin):
    assert "tencent" in admin.supports("HK")
    assert "akshare" not in admin.supports("US")
    assert set(admin.implementations()) >= {"tencent", "sina"}


class FakeGet:
    """只替掉网络那一跳：根数折算与区间裁剪仍走真实实现。"""

    def __init__(self, payload):
        self.payload = payload

    def __call__(self, url, params, *, timeout=15):
        return self.payload


def rows(*days):
    return [
        {"day": day, "open": "1", "high": "2", "low": "0.5", "close": "1.5", "volume": "1000"}
        for day in days
    ]


def test_probe_returns_rows_and_the_window_it_saw(admin, monkeypatch):
    monkeypatch.setattr(
        "sams.services.sources.build",
        lambda kind, options=None: SinaAdapter(get=FakeGet(rows("2024-01-02", "2024-01-03"))),
    )
    admin.add("sx", "sina", ["A"])
    result = admin.probe("sx", "A", "600519", START, END)
    assert result.ok and result.rows == 2
    assert (result.first_date, result.last_date) == (date(2024, 1, 2), date(2024, 1, 3))
    assert "取到 2 根" in result.verdict


def test_probe_reports_the_failure_instead_of_raising(admin, monkeypatch):
    """试跑的价值在于「问一句就知道它怎么坏的」：抛异常会把原因藏进栈里。"""
    monkeypatch.setattr(
        "sams.services.sources.build",
        lambda kind, options=None: SinaAdapter(get=FakeGet({"not": "a list"})),
    )
    admin.add("sx2", "sina", ["A"])
    result = admin.probe("sx2", "A", "600519", START, END)
    assert not result.ok
    assert "DatasourceError" in result.error and "不是数组" in result.error
    assert result.verdict == result.error


def test_probe_only_constructs_the_source_it_was_asked_about(admin, monkeypatch):
    """全链跑通不代表每个源都好：被备源救回来的那次失败在 health 里看不见。"""
    called = []

    def fake_build(kind, options=None):
        called.append(kind)
        return SinaAdapter(get=FakeGet([]))

    monkeypatch.setattr("sams.services.sources.build", fake_build)
    result = admin.probe("sina", "A", "600519", START, END)
    assert called == ["sina"], "试跑必须绕开降级链"
    assert not result.ok and "EmptyResponse" in result.error


def test_probe_refuses_an_unknown_name_or_an_illegal_route(admin):
    with pytest.raises(ValidationError, match="不存在"):
        admin.probe("ghost", "A", "600519", START, END)
    with pytest.raises(ValidationError, match="不支持市场"):
        admin.probe("akshare", "HK", "00700", START, END)


def test_sample_bars_still_describes_the_standard_frame():
    """上面的用例不碰它，但它是全项目共用的样本：形态变了要在这里红。"""
    frame = sample_bars()
    assert frame.height == 2 and "source" in frame.columns
