from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from sams.core.errors import StorageError, ValidationError
from sams.storage.repos.corp_actions import CorpAction, CorpActionRepo

D = date(2024, 6, 14)


@pytest.fixture
def repo(db) -> CorpActionRepo:
    return CorpActionRepo(db)


def _action(**kwargs) -> CorpAction:
    base = {
        "market": "A",
        "code": "600519",
        "ex_date": D,
        "kind": "dividend",
        "src": "eastmoney",
        "cash_per_share": 30.0,
    }
    return CorpAction(**(base | kwargs))


def test_upsert_is_idempotent(repo):
    repo.upsert(_action())
    repo.upsert(_action())
    assert repo.count() == 1, "同一天同类型的第二次入库是修正，不是第二条记录"


def test_same_ex_date_different_kind_coexist(repo):
    repo.upsert(_action())
    repo.upsert(_action(kind="bonus", cash_per_share=None, ratio=1.5))
    assert repo.count() == 2, "同一天既派息又送股是真实存在的情形"


# --- 同一主键上两个不同的数：宁可拒绝写入 ------------------------------------------
#
# 主键是 (market, code, ex_date, kind)，`src` 不在里面。对抗测试查出的唯一一条「三道闸
# 全过、出门一个错数」：库里已有年度派息 2.0，备源说同日 3.0，覆盖写就只剩 3.0——
# 区间涨跌读起来完全正常（−5.13%，实际 0.00%），守卫看不出任何异常，因为库里那条记录
# 自洽得很。修不了主键（改主键要新迁移，且幂等重放会变成重复累加），先把静默改掉。


def test_a_second_amount_for_the_same_day_and_kind_refuses_to_overwrite(repo):
    repo.upsert(_action(cash_per_share=2.0))
    with pytest.raises(StorageError, match="冲突"):
        repo.upsert(_action(cash_per_share=3.0, src="eastmoney"))
    assert repo.list_for("A", "600519")["cash_per_share"].to_list() == [2.0], (
        "拒绝写入必须真的不动库：写一半再抛，库里留下的就是「谁都没确认过」的那条"
    )


def test_the_refusal_names_both_claims_and_says_what_to_do(repo):
    repo.upsert(_action(cash_per_share=2.0, src="akshare"))
    with pytest.raises(StorageError) as excinfo:
        repo.upsert(_action(cash_per_share=3.0, src="eastmoney"))
    text = str(excinfo.value)
    assert "600519" in text and "2024-06-14" in text and "dividend" in text
    assert "akshare" in text and "eastmoney" in text
    assert "2.0" in text and "3.0" in text
    assert "区间收益" in text, "得让敲命令的人知道接下来这只票会拒绝出数，而不是只知道失败了"


def test_a_different_share_count_for_the_same_day_is_a_conflict_too(repo):
    repo.upsert(_action(kind="bonus", cash_per_share=None, ratio=1.5))
    with pytest.raises(StorageError, match="冲突"):
        repo.upsert(_action(kind="bonus", cash_per_share=None, ratio=1.4))


def test_a_rounding_difference_from_another_source_is_still_a_correction(repo):
    """2.10 与 2.099 说的是同一件事。把它们判成冲突会让 `sync actions` 天天失败。"""
    repo.upsert(_action(cash_per_share=2.10))
    repo.upsert(_action(cash_per_share=2.099, src="eastmoney"))
    assert repo.count() == 1
    assert repo.list_for("A", "600519")["cash_per_share"].to_list() == [2.099]


def test_the_same_numbers_under_another_src_replace_without_refusing(repo):
    repo.upsert(_action(cash_per_share=2.10, src="akshare"))
    repo.upsert(_action(cash_per_share=2.10, src="eastmoney"))
    assert repo.count() == 1
    assert repo.list_for("A", "600519")["src"].to_list() == ["eastmoney"]


def test_two_unpriceable_events_on_the_same_day_collapse_without_refusing(repo):
    """unknown 无论如何都让读取层拒绝（D39），留哪条都不影响出数：为它拒绝只是添乱。"""
    repo.upsert(_action(kind="unknown", cash_per_share=None, note="以股代息"))
    repo.upsert(_action(kind="unknown", cash_per_share=None, note="认股权证", src="eastmoney"))
    assert repo.count() == 1


def test_a_batch_with_a_conflict_writes_no_rows_at_all(repo):
    """整批要么全进要么全不进：先写好三条再在第四条上抛，等于留下一个「查过但不齐」的区间。

    那种区间最坏的地方是它看起来齐了——读取层只看除权记录，看不出这次采集断在半路。
    """
    repo.upsert(_action(cash_per_share=2.0))
    frame = pl.DataFrame(
        {
            "market": ["A", "A"],
            "code": ["600519", "600519"],
            "ex_date": [date(2024, 7, 1), D],
            "kind": ["bonus", "dividend"],
            "cash_per_share": [None, 3.0],
            "ratio": [1.5, None],
            "price": [None, None],
            "src": ["eastmoney", "eastmoney"],
            "note": ["10送5", "10派30元(含税)"],
        }
    )
    with pytest.raises(StorageError, match="冲突"):
        repo.upsert_many(frame)
    assert repo.count() == 1, "同批那条没有冲突的记录也不该先进库：库里还是这一条老的"
    assert repo.list_for("A", "600519")["cash_per_share"].to_list() == [2.0]


def test_an_unrecognized_kind_spelling_is_rejected(repo):
    with pytest.raises(ValidationError, match="未知公司行为类型"):
        repo.upsert(_action(kind="分紅"))


def test_an_unpriceable_action_is_stored_with_its_source_text(repo):
    """D39：unknown 是一种**记录**，不是缺数据——它带的原文是下游拒绝时的依据。"""
    repo.upsert(
        _action(
            kind="unknown", cash_per_share=None, ratio=None, price=None, note="以股代息，比例未披露"
        )
    )
    assert repo.count() == 1


def test_an_unpriceable_action_without_a_note_is_rejected(repo):
    with pytest.raises(ValidationError, match="原文"):
        repo.upsert(_action(kind="unknown", cash_per_share=None, note=None))


def test_an_unpriceable_action_carrying_a_number_is_rejected(repo):
    """能给出数值就说明知道它是哪一种，标 unknown 是在逃避类型校验。"""
    with pytest.raises(ValidationError, match="不该带数值"):
        repo.upsert(_action(kind="unknown", cash_per_share=None, ratio=1.5))


def test_action_without_amount_or_ratio_is_rejected(repo):
    """「给了个数」不再是充分条件：每个类型知道自己该给哪个数。"""
    with pytest.raises(ValidationError, match="cash_per_share"):
        repo.upsert(_action(cash_per_share=None))
    with pytest.raises(ValidationError, match="乘数"):
        repo.upsert(_action(kind="bonus", cash_per_share=None, ratio=None))


def test_point_in_time_read(repo):
    repo.upsert(_action())
    repo.upsert(_action(ex_date=date(2025, 6, 14)))
    assert repo.list_for("A", "600519", until=date(2024, 12, 31)).height == 1, (
        "as-of 读法是复权因子可重现的前提：站在 2024 年不该看见 2025 年的除权"
    )
    assert repo.list_for("A", "600519").height == 2


# --- 每种公司行为的字段契约（D34）-----------------------------------------------------
#
# ratio 的语义是「除权后股数 / 除权前股数」，不是「每股送几股」，也不是「拆股比例」——
# 一列三种理解，因子就会乘反。校验放在仓库层是因为读它的人（因子推导）没有第二次机会猜。


def test_dividend_must_not_carry_a_ratio(repo):
    with pytest.raises(ValidationError, match="dividend"):
        repo.upsert(_action(ratio=1.5))


def test_dividend_cash_must_be_positive(repo):
    with pytest.raises(ValidationError, match="正数"):
        repo.upsert(_action(cash_per_share=0.0))


def test_bonus_and_transfer_must_increase_share_count(repo):
    """送转只会增加股数；写成 0.5 是把这个数当成了「每股送 0.5 股」。"""
    for kind in ("bonus", "transfer"):
        with pytest.raises(ValidationError, match="大于 1"):
            repo.upsert(_action(kind=kind, cash_per_share=None, ratio=0.5))


def test_split_accepts_both_directions(repo):
    """拆股 1→2 与合股 2→1 都是合法乘数，但 1.0 意味着什么都没发生。"""
    repo.upsert(_action(kind="split", cash_per_share=None, ratio=2.0))
    repo.upsert(_action(kind="split", ex_date=date(2024, 7, 1), cash_per_share=None, ratio=0.5))
    with pytest.raises(ValidationError, match="拆合股"):
        repo.upsert(_action(kind="split", cash_per_share=None, ratio=1.0))


def test_rights_need_both_share_count_and_issue_price(repo):
    """配股没有配股价就推不出除权参考价——要付钱这件事不能丢。"""
    with pytest.raises(ValidationError, match="配股价"):
        repo.upsert(_action(kind="rights", cash_per_share=None, ratio=1.2))
    repo.upsert(_action(kind="rights", cash_per_share=None, ratio=1.2, price=8.5))


def test_issue_price_only_belongs_to_rights(repo):
    with pytest.raises(ValidationError, match="price"):
        repo.upsert(_action(kind="bonus", cash_per_share=None, ratio=1.5, price=8.5))


def test_price_is_returned_by_the_read(repo):
    repo.upsert(_action(kind="rights", cash_per_share=None, ratio=1.2, price=8.5))
    row = repo.list_for("A", "600519").to_dicts()[0]
    assert row["price"] == 8.5

