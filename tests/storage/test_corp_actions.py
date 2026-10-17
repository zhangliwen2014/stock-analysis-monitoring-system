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
    repo.upsert(_action(kind="bonus", cash_per_share=None, ratio=0.5))
    assert repo.count() == 2, "同一天既派息又送股是真实存在的情形"


def test_unknown_kind_is_rejected(repo):
    with pytest.raises(ValidationError, match="未知公司行为类型"):
        repo.upsert(_action(kind="分紅"))


def test_action_without_amount_or_ratio_is_rejected(repo):
    with pytest.raises(ValidationError, match="现金或比例"):
        repo.upsert(_action(cash_per_share=None))


def test_point_in_time_read(repo):
    repo.upsert(_action())
    repo.upsert(_action(ex_date=date(2025, 6, 14)))
    assert repo.list_for("A", "600519", until=date(2024, 12, 31)).height == 1, (
        "as-of 读法是复权因子可重现的前提：站在 2024 年不该看见 2025 年的除权"
    )
    assert repo.list_for("A", "600519").height == 2
