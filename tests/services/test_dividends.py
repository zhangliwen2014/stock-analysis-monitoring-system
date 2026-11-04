"""分红入账（S4-D §6.2）：除权日前持股 × 每股，幂等靠 `ref`。

分红不是「今天有几股就乘几股」：除权日那天才买进来的股不享有这次派息。
数量取**除权日前**（按市场本地日）已成交的累计持股，因此同一只票卖光再来一次除权，
第二次股数为 0、不入账；同一笔重跑则由 `(account, kind, ref)` 的唯一索引拦住。

税前的毛额：v0 不建模 A 股红利税，入账数与屏幕文案都按毛额说。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from sams.core.errors import StorageError
from sams.services.dividends import TAX_NOTE, DividendService
from sams.services.ledger import LedgerService
from sams.storage.db import Database
from sams.storage.repos.corp_actions import CorpAction, CorpActionRepo
from sams.storage.repos.ledger import SimAccountRepo, SimOrderRepo
from sams.storage.repos.symbols import SymbolRepo
from sams.storage.repos.theses import ThesisRepo
from sams.storage.repos.users import UserRepo

USER = "trader"
PWD = "correct-horse-battery"

#: 账户开在这一切之前：分红只入开立之后的除权日
OPENED_AT = datetime(2024, 1, 2, 1, 0, tzinfo=UTC)


@pytest.fixture
def books(db: Database, settings):
    accounts = SimAccountRepo(db)
    orders = SimOrderRepo(db, accounts, markets=settings.markets, symbols=SymbolRepo(db))
    users = UserRepo(db)
    users.create(USER, PWD)
    ledger = LedgerService(accounts, orders, ThesisRepo(db), users, settings)
    actions = CorpActionRepo(db)
    dividends = DividendService(accounts, orders, actions, settings)
    return db, ledger, actions, dividends


def _account(books, *, created_at: datetime = OPENED_AT) -> str:
    db, ledger, _, _ = books
    account = ledger.open_account(USER, "主账户")
    # 开立时刻决定「哪些除权日算这个账户的」：用例要验 2024 年的分红，就把开户时刻放回去
    db.execute("UPDATE sim_accounts SET created_at = ? WHERE id = ?", [created_at, account.id])
    ledger.deposit(account.id, "CNY", 100_000.0, username=USER, reason="初始", actor_id=USER)
    return account.id


def _buy(ledger: LedgerService, account_id: str, *, when: datetime, qty: float = 100) -> None:
    ledger.buy(
        account_id,
        USER,
        "A",
        "601088",
        qty=qty,
        price=30.0,
        ts=when,
    )


def _sell(ledger: LedgerService, account_id: str, *, when: datetime, qty: float = 100) -> None:
    ledger.sell(
        account_id,
        USER,
        "A",
        "601088",
        qty=qty,
        price=31.0,
        ts=when,
    )


def _dividend(
    actions: CorpActionRepo,
    *,
    ex_date: date,
    cash: float | None = 0.5,
    market: str = "A",
    code: str = "601088",
) -> None:
    actions.upsert(
        CorpAction(
            market=market,
            code=code,
            ex_date=ex_date,
            kind="dividend",
            src="test",
            cash_per_share=cash,
        )
    )


def _cash(ledger: LedgerService, account_id: str) -> float:
    return ledger.accounts.cash_balances(account_id)["CNY"]


def test_dividend_is_the_shares_held_before_the_ex_date_times_per_share(books) -> None:
    _, ledger, actions, dividends = books
    account_id = _account(books)
    _buy(ledger, account_id, when=datetime(2024, 6, 3, 15, 0, tzinfo=UTC))
    _dividend(actions, ex_date=date(2024, 7, 8), cash=0.5)
    before = _cash(ledger, account_id)

    report = dividends.post(account_id, date(2024, 7, 8))

    assert [row.amount for row in report.postings] == [50.0]
    assert [row.qty for row in report.postings] == [100.0]
    assert report.postings[0].currency == "CNY"
    assert _cash(ledger, account_id) == pytest.approx(before + 50.0)


def test_posting_again_does_not_duplicate_the_same_dividend(books) -> None:
    _, ledger, actions, dividends = books
    account_id = _account(books)
    _buy(ledger, account_id, when=datetime(2024, 6, 3, 15, 0, tzinfo=UTC))
    _dividend(actions, ex_date=date(2024, 7, 8), cash=0.5)
    dividends.post(account_id, date(2024, 7, 8))
    after_first = _cash(ledger, account_id)

    again = dividends.post(account_id, date(2024, 7, 8))

    assert again.postings == ()
    assert again.skipped_existing == 1
    assert _cash(ledger, account_id) == pytest.approx(after_first)


def test_shares_bought_on_the_ex_date_do_not_receive_it(books) -> None:
    """除权日当天开盘前成交的股（按市场本地日仍是当天）不享有这次派息。"""
    _, ledger, actions, dividends = books
    account_id = _account(books)
    # 2024-07-07T16:30Z = 2024-07-08 00:30 +08：UTC 看还是前一天，A 股本地已是除权日
    _buy(ledger, account_id, when=datetime(2024, 7, 7, 16, 30, tzinfo=UTC))
    _dividend(actions, ex_date=date(2024, 7, 8), cash=0.5)

    report = dividends.post(account_id, date(2024, 7, 8))

    assert report.postings == ()
    assert report.skipped_no_shares == 1


def test_selling_out_before_a_later_ex_date_posts_nothing(books) -> None:
    _, ledger, actions, dividends = books
    account_id = _account(books)
    _buy(ledger, account_id, when=datetime(2024, 6, 3, 15, 0, tzinfo=UTC))
    _dividend(actions, ex_date=date(2024, 7, 8), cash=0.5)
    _dividend(actions, ex_date=date(2024, 9, 9), cash=0.4)
    first = dividends.post(account_id, date(2024, 7, 8))
    _sell(ledger, account_id, when=datetime(2024, 8, 1, 15, 0, tzinfo=UTC))
    after_sale = _cash(ledger, account_id)

    second = dividends.post(account_id, date(2024, 9, 9))

    assert [row.ex_date for row in first.postings] == [date(2024, 7, 8)]
    assert second.postings == ()
    assert second.skipped_no_shares == 1
    assert _cash(ledger, account_id) == pytest.approx(after_sale)


def test_a_hong_kong_dividend_lands_in_hong_kong_dollars(books) -> None:
    _, ledger, actions, dividends = books
    account_id = _account(books)
    ledger.deposit(account_id, "HKD", 100_000.0, username=USER, reason="港币", actor_id=USER)
    ledger.buy(
        account_id,
        USER,
        "HK",
        "00700",
        qty=200,
        price=300.0,
        ts=datetime(2024, 5, 6, 7, 0, tzinfo=UTC),
    )
    _dividend(actions, ex_date=date(2024, 5, 17), cash=3.4, market="HK", code="00700")

    report = dividends.post(account_id, date(2024, 5, 17))

    assert [row.currency for row in report.postings] == ["HKD"]
    assert [row.amount for row in report.postings] == [680.0]
    assert ledger.accounts.cash_balances(account_id)["HKD"] == pytest.approx(
        100_000.0 - 200 * 300.0 + 680.0
    )


def test_a_market_filter_only_posts_that_market(books) -> None:
    _, ledger, actions, dividends = books
    account_id = _account(books)
    _buy(ledger, account_id, when=datetime(2024, 6, 3, 15, 0, tzinfo=UTC))
    ledger.deposit(account_id, "HKD", 100_000.0, username=USER, reason="港币", actor_id=USER)
    ledger.buy(
        account_id,
        USER,
        "HK",
        "00700",
        qty=200,
        price=300.0,
        ts=datetime(2024, 6, 3, 7, 0, tzinfo=UTC),
    )
    _dividend(actions, ex_date=date(2024, 7, 8), cash=0.5)
    _dividend(actions, ex_date=date(2024, 7, 9), cash=3.4, market="HK", code="00700")

    only_a = dividends.post(account_id, date(2024, 7, 9), market="A")
    only_hk = dividends.post(account_id, date(2024, 7, 9), market="HK")

    assert [(row.market, row.code) for row in only_a.postings] == [("A", "601088")]
    assert [(row.market, row.code) for row in only_hk.postings] == [("HK", "00700")]


def test_an_account_opened_after_the_ex_date_gets_nothing(books) -> None:
    _, ledger, actions, dividends = books
    _dividend(actions, ex_date=date(2024, 7, 8), cash=0.5)
    account_id = _account(books, created_at=datetime(2024, 8, 1, 1, 0, tzinfo=UTC))

    report = dividends.post(account_id, date(2024, 9, 1))

    assert report.postings == ()


def test_a_dividend_row_without_cash_per_share_is_skipped_with_a_warning(books) -> None:
    """写入层已拒这种行；读侧仍要兜住旧数据：跳过、说清楚，不乘出一个 0。"""
    db, ledger, _, dividends = books
    account_id = _account(books)
    _buy(ledger, account_id, when=datetime(2024, 6, 3, 15, 0, tzinfo=UTC))
    db.execute(
        "INSERT INTO corp_actions (market, code, ex_date, kind, src, cash_per_share, note)"
        " VALUES ('A', '601088', '2024-07-08', 'dividend', 'old', NULL, '旧行')"
    )

    report = dividends.post(account_id, date(2024, 7, 8))

    assert report.postings == ()
    assert any("cash_per_share" in warning for warning in report.warnings)


def test_the_tax_note_says_gross_before_dividend_tax() -> None:
    assert "税前" in TAX_NOTE
    assert "红利税" in TAX_NOTE


def test_an_unknown_account_is_refused(books) -> None:
    _, _, _, dividends = books
    with pytest.raises(StorageError):
        dividends.post("not-an-account", date(2024, 7, 8))
