"""账本仓库：状态前置条件在 storage 层强制（D81 的形状，同一个理由）。"""

from __future__ import annotations

from datetime import datetime

import pytest

from sams.core.errors import StorageError, ValidationError
from sams.storage.db import Database
from sams.storage.repos.ledger import Account, SimAccountRepo

USER = "u1"


@pytest.fixture
def accounts(db: Database) -> SimAccountRepo:
    return SimAccountRepo(db)


def test_opening_an_account_returns_the_row_we_can_read_back(accounts: SimAccountRepo) -> None:
    account = accounts.open(USER, "主账户", report_currency="CNY")
    assert isinstance(account, Account)
    assert accounts.get(USER, account.id).name == "主账户"
    assert accounts.list_for(USER)[0].id == account.id


def test_two_users_can_have_accounts_with_the_same_name(accounts: SimAccountRepo) -> None:
    """名字的唯一键是 (user_id, name)：`0001` 就是这么定的，仓库不许放宽。"""
    accounts.open(USER, "主账户", report_currency="CNY")
    accounts.open("u2", "主账户", report_currency="CNY")
    assert len(accounts.list_for(USER)) == 1


def test_a_duplicate_name_for_one_user_is_a_business_refusal(accounts: SimAccountRepo) -> None:
    """不是 ValidationError：重名是「这一步不让走」，不是「参数打错了」（D77）。"""
    accounts.open(USER, "主账户", report_currency="CNY")
    with pytest.raises(StorageError, match="已存在"):
        accounts.open(USER, "主账户", report_currency="CNY")


def test_an_empty_account_name_is_a_typo(accounts: SimAccountRepo) -> None:
    with pytest.raises(ValidationError, match="空"):
        accounts.open(USER, "  ", report_currency="CNY")


def test_another_user_cannot_read_the_account(accounts: SimAccountRepo) -> None:
    """用户隔离与 watchlists/theses 同形：没有「查全量再由上层过滤」的接口。"""
    account = accounts.open(USER, "主账户", report_currency="CNY")
    assert accounts.get("u2", account.id) is None
    assert accounts.list_for("u2") == []


def test_cash_starts_empty_per_currency_not_zero_everywhere(accounts: SimAccountRepo) -> None:
    account_id = accounts.open(USER, "A", report_currency="CNY").id
    assert accounts.cash_balances(account_id) == {}
    accounts.adjust_cash(account_id, "CNY", 100_000.0, reason="初始资金", actor_id=USER)
    assert accounts.cash_balances(account_id) == {"CNY": pytest.approx(100_000.0)}


def test_every_cash_move_leaves_an_audit_row(accounts: SimAccountRepo) -> None:
    """§5.4 规则 4 的 M3 版本：换汇留 M5，但**手工**动现金从第一天起就得有出处。"""
    account_id = accounts.open(USER, "A", report_currency="CNY").id
    accounts.adjust_cash(account_id, "CNY", 100_000.0, reason="初始资金", actor_id=USER)
    accounts.adjust_cash(account_id, "CNY", -20_000.0, reason="转出", actor_id=USER)
    history = accounts.cash_history(account_id)
    assert history["delta"].to_list() == [100_000.0, -20_000.0]
    assert history["reason"].to_list() == ["初始资金", "转出"]


def test_balances_and_moves_are_written_together_or_not_at_all(accounts: SimAccountRepo) -> None:
    """留一行没有余额变动的流水，比留一个对不上流水的余额更难查。

    这里用「给不存在的账户调现金」触发失败：写流水在前、更新余额在后时，
    前半段会留在库里。
    """
    with pytest.raises(StorageError):
        accounts.adjust_cash("不存在", "CNY", 10.0, reason="试探", actor_id=USER)
    assert accounts.cash_history("不存在").height == 0


def test_cash_cannot_go_negative_without_margin(accounts: SimAccountRepo) -> None:
    account_id = accounts.open(USER, "A", report_currency="CNY").id
    accounts.adjust_cash(account_id, "CNY", 100.0, reason="初始", actor_id=USER)
    with pytest.raises(StorageError, match="现金不足"):
        accounts.adjust_cash(account_id, "CNY", -101.0, reason="透支", actor_id=USER)
    assert accounts.cash_balances(account_id)["CNY"] == pytest.approx(100.0)


def test_a_margin_account_may_go_negative(accounts: SimAccountRepo) -> None:
    """`margin_enabled` 是 0001 就有的列；不认它等于把这张列变成装饰。"""
    account_id = accounts.open(
        USER, "杠杆", report_currency="CNY", margin_enabled=True
    ).id
    accounts.adjust_cash(account_id, "CNY", -100.0, reason="融资", actor_id=USER)
    assert accounts.cash_balances(account_id)["CNY"] == pytest.approx(-100.0)


def test_an_empty_reason_is_refused(accounts: SimAccountRepo) -> None:
    """审计行没有理由就不是审计：留 `""` 会让这张表在第一周就变成没人读的日志。"""
    account_id = accounts.open(USER, "A", report_currency="CNY").id
    with pytest.raises(ValidationError, match="理由"):
        accounts.adjust_cash(account_id, "CNY", 1.0, reason="", actor_id=USER)


def test_a_zero_delta_is_refused(accounts: SimAccountRepo) -> None:
    """0 不是「没动」的合法表达，它是一条会出现在审计报告里的假流水。"""
    account_id = accounts.open(USER, "A", report_currency="CNY").id
    with pytest.raises(ValidationError, match="0"):
        accounts.adjust_cash(account_id, "CNY", 0.0, reason="占位", actor_id=USER)


def test_an_unknown_currency_is_refused(accounts: SimAccountRepo) -> None:
    account_id = accounts.open(USER, "A", report_currency="CNY").id
    with pytest.raises(ValidationError, match="币种"):
        accounts.adjust_cash(account_id, "RMB", 1.0, reason="试探", actor_id=USER)


def test_a_bare_timestamp_is_refused(accounts: SimAccountRepo) -> None:
    """TIMESTAMPTZ 会按会话时区解释无时区的值：A 股的一笔会被记成 UTC，差 8 小时。"""
    account_id = accounts.open(USER, "T", report_currency="CNY").id
    with pytest.raises(ValidationError, match="时区"):
        accounts.adjust_cash(
            account_id, "CNY", 1.0, reason="试探", actor_id=USER,
            ts=datetime(2024, 10, 25, 15, 0),  # 裸时刻
        )
