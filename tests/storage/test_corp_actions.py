from __future__ import annotations

from datetime import date

import pytest

from sams.core.errors import ValidationError
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


def test_upsert_is_idempotent(db, repo):
    repo.upsert(_action())
    repo.upsert(_action())
    assert repo.count() == 1, "同一天同类型的第二次入库是修正，不是第二条记录"


def test_same_ex_date_different_kind_coexist(repo):
    repo.upsert(_action())
    repo.upsert(_action(kind="bonus", cash_per_share=None, ratio=1.5))
    assert repo.count() == 2, "同一天既派息又送股是真实存在的情形"


def test_unknown_kind_is_rejected(repo):
    with pytest.raises(ValidationError, match="未知公司行为类型"):
        repo.upsert(_action(kind="分紅"))


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

