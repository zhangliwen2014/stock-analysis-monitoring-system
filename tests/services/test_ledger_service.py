"""账本写用例（spec §5.4、§9.1 的 thesis↔成交那条链）。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sams.core.errors import StorageError, ValidationError
from sams.services.ledger import LedgerService
from sams.storage.db import Database
from sams.storage.repos.ledger import SimAccountRepo, SimOrderRepo
from sams.storage.repos.symbols import SymbolRepo
from sams.storage.repos.theses import ThesisRepo

USER = "u1"
#: 立论那三步要落时刻（0013 的测量日、时间线的 ts），测试里冻死它，不看真实钟表
NOW = datetime(2024, 10, 26, 2, 0, tzinfo=UTC)


@pytest.fixture
def svc(db: Database, settings) -> LedgerService:
    accounts = SimAccountRepo(db)
    orders = SimOrderRepo(db, accounts, markets=settings.markets, symbols=SymbolRepo(db))
    return LedgerService(accounts, orders, ThesisRepo(db), settings)


def _funded(svc: LedgerService, name: str) -> str:
    account = svc.open_account(USER, name)
    svc.deposit(account.id, "CNY", 100_000.0, reason="初始", actor_id=USER)
    return account.id


def _opened_thesis(db: Database, user: str = USER) -> str:
    """走到 opened 的一条立论：挂成交要的就是这个状态。"""
    repo = ThesisRepo(db)
    thesis = repo.create(
        user_id=user, market="A", code="601088", style="PR", actor_id=user, now=NOW
    )
    repo.advance(user, thesis.id, "proposed", actor_id=user, now=NOW)
    repo.set_levels(
        user,
        thesis.id,
        actor_id=user,
        now=NOW,
        entry_low=38.0,
        entry_high=40.0,
        stop_price=34.0,
        target_price=50.0,
        reward_risk=1.8,
    )
    repo.advance(user, thesis.id, "opened", actor_id=user, now=NOW)
    return thesis.id


def test_open_account_delegates_with_the_configured_report_currency(svc) -> None:
    account = svc.open_account(USER, "主账户")
    assert account.report_currency == "CNY"


def test_buy_derives_the_currency_from_market_and_code(svc) -> None:
    """调用方不必也不应自己传币种：传了就要防着传错，不传则只有一条推导链。"""
    account_id = _funded(svc, "A")
    order = svc.buy(account_id, USER, "A", "601088", qty=100, price=40.0, fee=5.0)
    assert order.currency == "CNY"


def test_the_hk_counter_part_derives_hkd(svc) -> None:
    """同一份代码要按市场给出不同币种，否则「推定」其实只是写死了 CNY。"""
    account = svc.open_account(USER, "HK")
    svc.deposit(account.id, "HKD", 100_000.0, reason="初始", actor_id=USER)
    order = svc.buy(account.id, USER, "HK", "700", qty=100, price=400.0)
    assert order.currency == "HKD"


def test_buy_refuses_when_cash_is_short_with_the_amount_in_the_message(svc) -> None:
    account = svc.open_account(USER, "B")
    svc.deposit(account.id, "CNY", 1_000.0, reason="初始", actor_id=USER)
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
    thesis = ThesisRepo(db).create(
        user_id=USER, market="A", code="601088", style="PR", actor_id=USER, now=NOW
    )
    with pytest.raises(ValidationError, match="observing"):
        svc.buy(account_id, USER, "A", "601088", qty=100, price=40.0, thesis_id=thesis.id)


def test_linking_a_fill_to_someone_elses_thesis_is_refused(svc, db) -> None:
    account_id = _funded(svc, "E")
    other = ThesisRepo(db).create(
        user_id="u2", market="A", code="601088", style="PR", actor_id="u2", now=NOW
    )
    with pytest.raises(ValidationError, match="不属于"):
        svc.buy(account_id, USER, "A", "601088", qty=100, price=40.0, thesis_id=other.id)


def test_an_opened_thesis_accepts_the_link(svc, db) -> None:
    account_id = _funded(svc, "F")
    thesis_id = _opened_thesis(db)
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
    after = svc.withdraw(account_id, "CNY", 500.0, reason="取出", actor_id=USER)
    assert after == 99_500.0
    assert svc.cash(account_id) == {"CNY": 99_500.0}
