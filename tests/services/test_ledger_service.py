"""账本写用例（spec §5.4、§9.1 的 thesis↔成交那条链）。

`--user` 在屏幕上是一个用户名，库里认的是 `users.id`。这一步解析放在服务层（同
`ThesisService._actor`）：放在命令层就成了「每个入口记得查一遍」，而漏掉的那个不会报错，
只会留下一条挂在不存在的用户身上的账——`buy --thesis` 那条链也因此必须走同一个 id。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sams.core.errors import StorageError, ValidationError
from sams.services.ledger import LedgerService
from sams.storage.db import Database
from sams.storage.repos.ledger import SimAccountRepo, SimOrderRepo
from sams.storage.repos.symbols import SymbolRepo
from sams.storage.repos.theses import ThesisRepo
from sams.storage.repos.users import UserRepo

USER = "trader"
PWD = "correct-horse-battery"
#: 立论那三步要落时刻（0013 的测量日、时间线的 ts），测试里冻死它，不看真实钟表
NOW = datetime(2024, 10, 26, 2, 0, tzinfo=UTC)


@pytest.fixture
def svc(db: Database, settings) -> LedgerService:
    accounts = SimAccountRepo(db)
    orders = SimOrderRepo(db, accounts, markets=settings.markets, symbols=SymbolRepo(db))
    users = UserRepo(db)
    users.create(USER, PWD)
    return LedgerService(accounts, orders, ThesisRepo(db), users, settings)


def _uid(svc: LedgerService, username: str = USER) -> str:
    """仓库层要的那个 id：用例里直接写仓库时得用它，不能用用户名。"""
    return svc.users.get_by_username(username).id


def _funded(svc: LedgerService, name: str) -> str:
    account = svc.open_account(USER, name)
    svc.deposit(account.id, "CNY", 100_000.0, username=USER, reason="初始", actor_id=USER)
    return account.id


def _opened_thesis(db: Database, user_id: str) -> str:
    """走到 opened 的一条立论：挂成交要的就是这个状态。"""
    repo = ThesisRepo(db)
    thesis = repo.create(
        user_id=user_id, market="A", code="601088", style="PR", actor_id=user_id, now=NOW
    )
    repo.advance(user_id, thesis.id, "proposed", actor_id=user_id, now=NOW)
    repo.set_levels(
        user_id,
        thesis.id,
        actor_id=user_id,
        now=NOW,
        entry_low=38.0,
        entry_high=40.0,
        stop_price=34.0,
        target_price=50.0,
        reward_risk=1.8,
    )
    repo.advance(user_id, thesis.id, "opened", actor_id=user_id, now=NOW)
    return thesis.id


def test_open_account_delegates_with_the_configured_report_currency(svc) -> None:
    account = svc.open_account(USER, "主账户")
    assert account.report_currency == "CNY"


def test_a_username_that_is_not_in_the_library_is_refused_with_the_fix(svc) -> None:
    """不是 ValidationError 就错了：`--user trder` 是参数打错了（2），不是业务拒绝。"""
    with pytest.raises(ValidationError, match="sams user add"):
        svc.open_account("trder", "主账户")


def test_a_disabled_user_is_refused(svc) -> None:
    """「禁用即停止一切动作」要覆盖记账：只对读生效的禁用等于没禁用。"""
    svc.users.disable(_uid(svc))
    with pytest.raises(ValidationError, match="禁用"):
        svc.open_account(USER, "主账户")


def test_writing_into_another_users_account_is_refused(svc, db) -> None:
    """仓库层的 `_require` 只查存在、不查归属：绕过服务就写得进别人的账。"""
    other = SimAccountRepo(db).open("someone-else", "别人的账户", report_currency="CNY")
    account_id = _funded(svc, "我的")
    with pytest.raises(StorageError, match="不属于"):
        svc.buy(other.id, USER, "A", "601088", qty=100, price=40.0)
    assert svc.cash(account_id) == {"CNY": 100_000.0}


def test_rename_account_resolves_the_username_and_reads_back(svc) -> None:
    """屏幕上写的是用户名，库里认 id：改名和其它写面走同一道解析（D204）。"""
    account = svc.open_account(USER, "文-中国银行")
    renamed = svc.rename_account(USER, account.id, "文-中国银河")
    assert renamed.name == "文-中国银河"
    assert [item.name for item in svc.accounts.list_for(_uid(svc))] == ["文-中国银河"]


def test_renaming_another_users_account_is_refused(svc, db) -> None:
    other = SimAccountRepo(db).open("someone-else", "别人的账户", report_currency="CNY")
    with pytest.raises(StorageError, match="不属于"):
        svc.rename_account(USER, other.id, "抢来的名字")
    assert SimAccountRepo(db).get("someone-else", other.id).name == "别人的账户"


def test_deposit_into_an_unknown_account_is_refused(svc) -> None:
    """1 不是 2：`--account` 这个参数本身没写错，错的是库里没有这笔账。"""
    with pytest.raises(StorageError, match="不存在"):
        svc.deposit(
            "no-such-account",
            "CNY",
            100.0,
            username=USER,
            reason="初始",
            actor_id=USER,
        )


def test_buy_derives_the_currency_from_market_and_code(svc) -> None:
    """调用方不必也不应自己传币种：传了就要防着传错，不传则只有一条推导链。"""
    account_id = _funded(svc, "A")
    order = svc.buy(account_id, USER, "A", "601088", qty=100, price=40.0, fee=5.0)
    assert order.currency == "CNY"


def test_the_hk_counter_part_derives_hkd(svc) -> None:
    """同一份代码要按市场给出不同币种，否则「推定」其实只是写死了 CNY。"""
    account = svc.open_account(USER, "HK")
    svc.deposit(account.id, "HKD", 100_000.0, username=USER, reason="初始", actor_id=USER)
    order = svc.buy(account.id, USER, "HK", "700", qty=100, price=400.0)
    assert order.currency == "HKD"


def test_buy_refuses_when_cash_is_short_with_the_amount_in_the_message(svc) -> None:
    account = svc.open_account(USER, "B")
    svc.deposit(account.id, "CNY", 1_000.0, username=USER, reason="初始", actor_id=USER)
    with pytest.raises(StorageError, match="现金不足"):
        svc.buy(account.id, USER, "A", "601088", qty=100, price=40.0)


def test_an_unknown_market_is_refused_before_any_write(svc) -> None:
    """D84 的同一条：没配置的市场连币种都推不出来，不该出现半笔账。"""
    account_id = _funded(svc, "C")
    with pytest.raises(ValidationError, match="SG"):
        svc.buy(account_id, USER, "SG", "D05", qty=100, price=1.0)
    assert svc.accounts_of(USER)[0].id == account_id
    assert svc.cash(account_id) == {"CNY": 100_000.0}


def test_linking_a_fill_to_a_non_opened_thesis_is_refused(svc, db) -> None:
    """「还在观察的观点」被记了一笔成交，时间线就变成假的（§9.1，D81 的同一动机）。"""
    account_id = _funded(svc, "D")
    repo = ThesisRepo(db)
    thesis = repo.create(
        user_id=_uid(svc), market="A", code="601088", style="PR", actor_id=_uid(svc), now=NOW
    )
    with pytest.raises(ValidationError, match="observing"):
        svc.buy(account_id, USER, "A", "601088", qty=100, price=40.0, thesis_id=thesis.id)


def test_linking_a_fill_to_someone_elses_thesis_is_refused(svc, db) -> None:
    """thesis 与账本认的是同一个 id：两条链各认各的身份，挂接就形同虚设。"""
    account_id = _funded(svc, "E")
    users: UserRepo = svc.users
    other_user = users.create("shortseller", PWD)
    other = ThesisRepo(db).create(
        user_id=other_user.id,
        market="A",
        code="601088",
        style="PR",
        actor_id=other_user.id,
        now=NOW,
    )
    with pytest.raises(ValidationError, match="不属于"):
        svc.buy(account_id, USER, "A", "601088", qty=100, price=40.0, thesis_id=other.id)


def test_an_opened_thesis_accepts_the_link(svc, db) -> None:
    account_id = _funded(svc, "F")
    thesis_id = _opened_thesis(db, _uid(svc))
    order = svc.buy(account_id, USER, "A", "601088", qty=100, price=40.0, thesis_id=thesis_id)
    assert order.thesis_id == thesis_id


def test_a_sell_takes_the_currency_from_the_position_not_the_caller(svc) -> None:
    """卖出记回哪个币种，是持仓定的；让它传参就多一条能传错的路。"""
    account_id = _funded(svc, "G")
    svc.buy(account_id, USER, "A", "601088", qty=100, price=40.0)
    order = svc.sell(account_id, USER, "A", "601088", qty=50, price=44.0)
    assert order.currency == "CNY"
    assert svc.position(account_id, "A", "601088").qty == 50


def test_withdraw_takes_the_absolute_value_so_a_negative_amount_cannot_top_up_cash(svc) -> None:
    """`withdraw(-100)` 若原样传下去会变成存钱：方向由方法名定，不由符号定。"""
    account_id = _funded(svc, "H")
    after = svc.withdraw(
        account_id, "CNY", 500.0, username=USER, reason="取出", actor_id=USER
    )
    assert after == 99_500.0
    assert svc.cash(account_id) == {"CNY": 99_500.0}
