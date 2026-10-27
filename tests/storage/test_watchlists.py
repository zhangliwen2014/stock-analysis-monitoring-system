from __future__ import annotations

import pytest

from sams.core.errors import ValidationError
from sams.storage.repos.watchlists import MAX_ITEMS_PER_USER, WatchlistRepo


@pytest.fixture
def repo(db) -> WatchlistRepo:
    return WatchlistRepo(db)


A_ID, B_ID = "user-a", "user-b"


def test_add_list_remove(repo):
    repo.add(A_ID, "A", "600519", note="白酒龙头")
    item = repo.list(A_ID)[0]
    assert (item.market, item.code, item.note) == ("A", "600519", "白酒龙头")
    assert repo.contains(A_ID, "A", "600519")
    assert repo.codes(A_ID) == [("A", "600519")]
    assert repo.remove(A_ID, "A", "600519") is True
    assert repo.remove(A_ID, "A", "600519") is False


def test_same_user_same_symbol_is_not_duplicated(repo):
    repo.add(A_ID, "A", "600519")
    repo.add(A_ID, "A", "600519", note="换备注")
    assert repo.count(A_ID) == 1
    assert repo.list(A_ID)[0].note == "换备注"


def test_user_b_never_sees_user_a_rows(repo):
    """用户隔离兜底在仓库层：每个查询都必须带 user_id 条件。"""
    repo.add(A_ID, "A", "600519")
    repo.add(B_ID, "HK", "00700")
    assert [i.code for i in repo.list(B_ID)] == ["00700"]
    assert repo.contains(B_ID, "A", "600519") is False
    assert repo.remove(B_ID, "A", "600519") is False
    assert repo.count(B_ID) == 1


def test_same_code_different_market_coexist(repo):
    repo.add(A_ID, "US", "AAPL")
    repo.add(A_ID, "HK", "AAPL")  # 不同市场同代码是两条自选
    assert len(repo.list(A_ID)) == 2


def test_admin_can_read_everything_but_not_write(repo):
    repo.add(A_ID, "A", "600519")
    repo.add(B_ID, "HK", "00700")
    rows = repo.list_all()
    assert {(i.user_id, i.code) for i in rows} == {(A_ID, "600519"), (B_ID, "00700")}
    for forbidden in ("add_for_user", "update", "delete", "remove_all"):
        assert not hasattr(repo, forbidden), f"admin 视图必须只读，不该有 {forbidden}"


def test_empty_user_id_is_rejected(repo):
    for call in (
        lambda: repo.add("", "A", "1"),
        lambda: repo.contains("", "A", "1"),
        lambda: repo.list(""),
        lambda: repo.remove("", "A", "1"),
    ):
        with pytest.raises(ValidationError, match="user_id"):
            call()


@pytest.mark.parametrize("market, code", [("", "600519"), ("A", ""), ("", "")])
def test_blank_market_or_code_rejected(repo, market, code):
    with pytest.raises(ValidationError, match="market"):
        repo.add(A_ID, market, code)


def test_per_user_cap(repo):
    for index in range(MAX_ITEMS_PER_USER):
        repo.add(A_ID, "A", f"{index:06d}")
    assert repo.count(A_ID) == MAX_ITEMS_PER_USER
    with pytest.raises(ValidationError, match="上限"):
        repo.add(A_ID, "A", "over-cap")
    assert repo.count(A_ID) == MAX_ITEMS_PER_USER, "超限的写入不能污染计数"


def test_codes_are_canonicalised_on_the_way_in(repo):
    """清单里的键必须和 bars 里的键同一个形态，否则那一行永远读不到行情且不报错。

    用户在界面上写 `700`、`HK.00700`、`00700.HK` 都是同一只票；采集侧入库的是 `00700`。
    """
    repo.add(A_ID, "HK", "700")
    assert [i.code for i in repo.list(A_ID)] == ["00700"]
    assert repo.contains(A_ID, "HK", "HK.00700")
    repo.add(A_ID, "HK", "HK00700")
    assert repo.count(A_ID) == 1, "同一只票的两种写法不该占两行"
    assert repo.remove(A_ID, "HK", "00700.HK") is True


def test_the_exchange_suffix_is_not_part_of_the_key(repo):
    repo.add(A_ID, "A", "601088.SH")
    assert [i.code for i in repo.list(A_ID)] == ["601088"]


def test_a_lowercase_market_lands_on_the_canonical_key(repo):
    """自选清单里的小写市场：加进去成功、列表里看得见，行情与告警一律读不到（D46 的下沉）。"""
    repo.add(A_ID, "a", "601088")
    assert repo.contains(A_ID, "A", "601088") is True
    assert [item.market for item in repo.list(A_ID)] == ["A"]
    assert repo.remove(A_ID, "A", "601088") is True


def test_remove_hides_the_row_but_keeps_it_for_the_sync_universe(repo):
    """折起只影响「看得到」，不影响「还要不要采」：`list_all()` 是采集清单的来源，
    它必须继续看见这一行，否则恢复那天起的历史就断了（D169）。"""
    repo.add(A_ID, "A", "600519", note="白酒龙头")
    assert repo.remove(A_ID, "A", "600519") is True
    assert repo.list(A_ID) == []
    assert repo.codes(A_ID) == []
    assert {(i.user_id, i.code) for i in repo.list_all()} == {(A_ID, "600519")}
    assert {(i.user_id, i.code) for i in repo.hidden(A_ID)} == {(A_ID, "600519")}


def test_restore_puts_it_back_with_the_note_it_had(repo):
    """一句话放回去：备注是用户写的那一条，不是恢复时新给的一个空串。"""
    repo.add(A_ID, "A", "600519", note="拿长线")
    repo.remove(A_ID, "A", "600519")
    assert repo.restore(A_ID, "A", "600519") is True
    assert repo.restore(A_ID, "A", "600519") is False, "没折起的行不叫恢复成功"
    assert repo.list(A_ID)[0].note == "拿长线"


def test_purge_really_deletes_the_row(repo):
    """`purge` 才是那一刀：折起的行继续占采集配额没意义，人明确要求删就得真删。"""
    repo.add(A_ID, "A", "600519")
    repo.remove(A_ID, "A", "600519")
    assert repo.purge(A_ID, "A", "600519") is True
    assert repo.purge(A_ID, "A", "600519") is False
    assert repo.list_all() == []


def test_adding_a_hidden_symbol_brings_it_back_with_the_new_note(repo):
    """重新加一只折起的票 ⇒ 回到看板并换备注，且不产生第二行（`INSERT OR REPLACE` 顺带把
    `removed_at` 抹回 NULL）。"""
    repo.add(A_ID, "A", "600519", note="旧备注")
    repo.remove(A_ID, "A", "600519")
    repo.add(A_ID, "A", "600519", note="新备注")
    assert repo.count(A_ID) == 1
    assert [(i.code, i.note) for i in repo.list(A_ID)] == [("600519", "新备注")]
    assert repo.hidden(A_ID) == []


def test_a_hidden_row_still_counts_against_the_per_user_cap(repo):
    """上限数的是「这只票在不在我的清单里」，折起来不腾出配额：
    所以 `count()` 不滤 `removed_at`，否则「折起 500 只再加 500 只」就成了绕闸门的后门。"""
    repo.add(A_ID, "A", "600519")
    repo.add(A_ID, "A", "601088")
    repo.remove(A_ID, "A", "600519")
    assert repo.count(A_ID) == 2
    assert [i.code for i in repo.list(A_ID)] == ["601088"]

