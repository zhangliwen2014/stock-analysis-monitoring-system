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
