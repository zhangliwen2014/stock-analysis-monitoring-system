"""数据源配置表：结构规则（名字、优先级不重复、级联删路由、确定性的链）。

这里只测 storage 层能负责的东西。「kind 是否是已注册实现」「停用它会不会让市场失明」
需要 datasource 与 Settings，分层上属于 services（见 tests/services/test_sources.py）。
"""

from __future__ import annotations

import duckdb
import pytest

from sams.core.errors import StorageError
from sams.storage.db import Database
from sams.storage.migrate import apply_migrations
from sams.storage.repos.data_sources import DataSourceRepo, SourceRow


@pytest.fixture
def repo(db) -> DataSourceRepo:
    return DataSourceRepo(db)


@pytest.fixture
def bare_db():
    """没跑过迁移的库：读侧要安静返回空，写侧要报 StorageError。"""
    database = Database(duckdb.connect(":memory:"))
    yield database
    database.close()


@pytest.fixture
def blank(repo, db) -> DataSourceRepo:
    """清掉种子的路由：这一组断言讲的是「我自己摆的链」，不是默认状态。"""
    db.execute("DELETE FROM data_source_routes")
    return repo


def row(name="alpha", kind="tencent", enabled=True, options=None, note=None) -> SourceRow:
    return SourceRow(name=name, kind=kind, enabled=enabled, options=options, note=note)


def test_migration_seeds_eight_sources_and_three_chains(repo):
    """种子的意义：老库跑完迁移就有备源，不需要谁来手工插记录。"""
    assert repo.chains() == {
        # akshare_ths 只报公司行为（D38）：东财那张分红送配表对 002450 这类票回
        # 「返回数据为空」，A 股因此不能只有单源——漏记一次派息是 D37 抓不住的（1~6% < 15%）
        # sina_quote 只报报价（D114）：它在两条数据链上都可见，但按能力过滤后
        # 日线链看不见它，报价链上它是腾讯之后的第二个故障域
        "A": ("akshare", "eastmoney", "tencent", "sina", "akshare_ths", "sina_quote"),
        "HK": ("yfinance", "tencent", "akshare_hk", "sina_quote"),
        "US": ("yfinance", "tencent", "sina_quote"),
    }
    assert len(repo.list()) == 8


def test_seeded_kinds_match_names(repo):
    for source in repo.list():
        assert source.kind == source.name, "种子用实现名当源名：多实例才需要另取名字"


def test_migrations_are_idempotent_for_the_seed(repo):
    apply_migrations(repo.db)
    assert len(repo.list()) == 8


def test_upsert_and_get_roundtrip_with_options(repo):
    repo.upsert(row(options={"timeout": 20}, note="第二实例"))
    got = repo.get("alpha")
    assert got.options == {"timeout": 20} and got.note == "第二实例"


def test_options_default_to_empty_dict(repo):
    repo.upsert(row())
    assert repo.get("alpha").options == {}


def test_upsert_replaces_in_place(repo):
    repo.upsert(row(enabled=True))
    repo.upsert(row(enabled=False, options={"timeout": 3}))
    assert repo.get("alpha") == row(enabled=False, options={"timeout": 3})


@pytest.mark.parametrize("bad", ["", "Alpha", "9alpha", "a b", "a" * 33, "akshare;drop"])
def test_names_must_be_quotable_identifiers(repo, bad):
    # 名字会进 CLI、进日志、进健康页：先规定成能被安全引用的形态
    with pytest.raises(StorageError, match="小写字母开头"):
        repo.upsert(row(name=bad))


def test_kind_is_validated_too(repo):
    with pytest.raises(StorageError):
        repo.upsert(row(kind="Tencent"))


def test_non_mapping_options_are_refused(repo):
    with pytest.raises(StorageError, match="必须是映射"):
        repo.upsert(SourceRow(name="alpha", kind="tencent", options=["timeout"]))


def test_route_requires_the_source_to_exist_first(repo):
    with pytest.raises(StorageError, match="先加源"):
        repo.set_route("A", "ghost", 1)


def test_priority_starts_at_one(blank):
    blank.upsert(row())
    with pytest.raises(StorageError, match="从 1 开始"):
        blank.set_route("A", "alpha", 0)


def test_taken_priority_is_refused_with_a_pointer_to_reorder(blank):
    blank.upsert(row(name="a1"))
    blank.upsert(row(name="a2"))
    blank.set_route("A", "a1", 1)
    with pytest.raises(StorageError, match="reorder"):
        blank.set_route("A", "a2", 1)


def test_reorder_writes_the_whole_chain_at_once(blank):
    """逐条 set_route 做不到插入：新位必被旧位占着。整体重写才是重排。"""
    for name in ("a1", "a2", "a3"):
        blank.upsert(row(name=name))
        blank.set_route("A", name, int(name[-1]))
    blank.reorder("A", ["a3", "a1", "a2"])
    assert blank.chains()["A"] == ("a3", "a1", "a2")
    assert [p for _, _, p in blank.routes()] == [1, 2, 3]


def test_chains_exclude_disabled_sources_but_routes_are_kept(blank):
    blank.upsert(row(name="a1"))
    blank.upsert(row(name="a2", enabled=False))
    blank.set_route("A", "a1", 1)
    blank.set_route("A", "a2", 2)
    assert blank.chains()["A"] == ("a1",)
    # 管理视图看得到停用源，否则「链里少一个」无法区分是被删还是被停
    assert [n for _, n, _ in blank.routes()] == ["a1", "a2"]
    blank.set_enabled("a2", True)
    assert blank.chains()["A"] == ("a1", "a2"), "重新启用回到原位次，不需要重排"


def test_same_priority_is_ordered_by_name_for_stability(blank):
    """同一位次由 `set_route` 挡着，但直接写库能做出来——那时链的次序必须是名字，
    不能是 DuckDB 恰好返回的顺序：降级顺序每次启动不一样的话，故障就无法复现。
    """
    for name in ("zz", "aa"):
        blank.upsert(row(name=name))
        blank.db.execute(
            "INSERT INTO data_source_routes (market, name, priority) VALUES ('A', ?, 1)", [name]
        )
    assert blank.chains()["A"] == ("aa", "zz")


def test_remove_cascades_routes(blank):
    blank.upsert(row())
    blank.set_route("A", "alpha", 1)
    blank.set_route("HK", "alpha", 1)
    assert blank.remove("alpha") == 2
    assert blank.routes() == [] and blank.get("alpha") is None


def test_remove_unknown_source_is_refused(repo):
    with pytest.raises(StorageError, match="不存在"):
        repo.remove("ghost")


def test_set_enabled_unknown_is_refused(repo):
    with pytest.raises(StorageError, match="不存在"):
        repo.set_enabled("ghost", False)


def test_remove_route_reports_whether_it_existed(blank):
    blank.upsert(row())
    blank.set_route("A", "alpha", 1)
    assert blank.remove_route("A", "alpha") is True
    assert blank.remove_route("A", "alpha") is False


def test_reads_are_quiet_and_writes_are_loud_without_the_table(bare_db):
    """老库（没跑过 0007）：读侧安静返回空让配置文件兜底，写侧必须说清表不在。"""
    fresh = DataSourceRepo(bare_db)
    assert fresh.list() == [] and fresh.routes() == [] and fresh.chains() == {}
    with pytest.raises(StorageError):
        fresh.upsert(row())
