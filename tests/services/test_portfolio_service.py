"""读用例：持仓的现价、盈亏、以及它们与汇率的关系（§5.4、D73）。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest
from support import FrozenClock

from sams.core.errors import ValidationError
from sams.services.portfolio import PortfolioService
from sams.storage.db import Database
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.fx import FxRepo
from sams.storage.repos.ledger import SimAccountRepo, SimOrderRepo
from sams.storage.repos.symbols import SymbolRepo

USER = "u1"
DAY = date(2024, 10, 25)
#: 冻住的「今天」：只有现金没有持仓时，换算锚在这一天（TODAY − DAY = 1，在陈旧窗口内）
TODAY = date(2024, 10, 26)


def _bars(market: str, code: str, closes: list[tuple[date, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "market": [market] * len(closes),
            "code": [code] * len(closes),
            "date": [c[0] for c in closes],
            "open": [c[1] for c in closes],
            "high": [c[1] for c in closes],
            "low": [c[1] for c in closes],
            "close": [c[1] for c in closes],
            "volume": [1000.0] * len(closes),
            "amount": [c[1] * 1000.0 for c in closes],
            "source": ["test"] * len(closes),
        }
    )


@pytest.fixture
def portfolio(db: Database, settings) -> PortfolioService:
    accounts = SimAccountRepo(db)
    orders = SimOrderRepo(db, accounts, markets=settings.markets, symbols=SymbolRepo(db))
    return PortfolioService(
        accounts,
        orders,
        BarRepo(db),
        FxRepo(db),
        clock=FrozenClock(datetime(2024, 10, 26, 2, 0, tzinfo=UTC)),
    )


def _funded_position(
    portfolio: PortfolioService,
    *,
    market: str = "A",
    code: str = "601088",
    qty: float = 100,
    price: float = 40.0,
    fx_rate_ref: float | None = None,
) -> str:
    currency = {"A": "CNY", "HK": "HKD", "US": "USD"}[market]
    account = portfolio.accounts.open(USER, f"账户 {market}", report_currency="CNY")
    portfolio.accounts.adjust_cash(
        account.id, currency, 1_000_000.0, reason="初始", actor_id=USER
    )
    portfolio.orders.record_fill(
        account_id=account.id,
        user_id=USER,
        market=market,
        code=code,
        side="buy",
        qty=qty,
        price=price,
        currency=currency,
        fee=0.0,
        fx_rate_ref=fx_rate_ref,
    )
    return account.id


def _us_position(portfolio: PortfolioService, *, fx_rate_ref: float | None = None) -> str:
    return _funded_position(
        portfolio, market="US", code="AAPL", qty=10, price=100.0, fx_rate_ref=fx_rate_ref
    )


def test_the_last_price_comes_from_the_raw_close(db, portfolio: PortfolioService) -> None:
    """D73：均价是成交日的真价格，现价取 hfq 会凭空造出一段从来没存在过的价差。"""
    account_id = _funded_position(portfolio, price=40.0)
    BarRepo(db).upsert(_bars("A", "601088", [(DAY - timedelta(days=1), 41.0), (DAY, 42.0)]))
    row = portfolio.positions(USER, account_id)[0]
    assert row.last_price == pytest.approx(42.0)
    assert row.last_price_date == DAY
    assert row.price_pnl == pytest.approx(200.0)


def test_a_position_with_no_bars_has_no_price_and_says_so(db, portfolio) -> None:
    """现价 None 而不是等于成本：后者把「今天没行情」显示成「今天持平」。"""
    account_id = _funded_position(portfolio)
    row = portfolio.positions(USER, account_id)[0]
    assert row.last_price is None
    assert row.price_pnl is None
    assert row.pnl is None
    assert "601088" in row.unavailable_note
    assert row.convertible is False


def test_a_cny_position_needs_no_rate(db, portfolio) -> None:
    account_id = _funded_position(portfolio, price=40.0)
    BarRepo(db).upsert(_bars("A", "601088", [(DAY, 44.0)]))
    row = portfolio.positions(USER, account_id)[0]
    assert row.fx_pnl == pytest.approx(0.0)
    assert row.total_report == pytest.approx(400.0)
    assert row.convertible is True


def test_the_two_pieces_still_add_up_when_a_rate_exists(db, portfolio) -> None:
    """同币种路径也要过恒等式：`price_pnl*rate_base + fx_pnl == total_report`。"""
    account_id = _funded_position(portfolio, price=40.0)
    BarRepo(db).upsert(_bars("A", "601088", [(DAY, 44.0)]))
    row = portfolio.positions(USER, account_id)[0]
    assert row.price_pnl * (row.rate_base or 1.0) + row.fx_pnl == pytest.approx(row.total_report)


def test_a_usd_position_splits_into_two_pieces(db, portfolio) -> None:
    """涨了 10 美元 + 人民币对美元贬了 4 毛，是两件事，必须显示成两个数（§5.4）。"""
    account_id = _funded_position(
        portfolio, market="US", code="AAPL", qty=10, price=100.0, fx_rate_ref=7.0
    )
    FxRepo(db).put(date(2024, 10, 1), "USD", "CNY", 7.0, source="manual")
    FxRepo(db).put(DAY, "USD", "CNY", 7.4, source="manual")
    BarRepo(db).upsert(_bars("US", "AAPL", [(DAY, 110.0)]))
    row = portfolio.positions(USER, account_id)[0]
    assert row.price_pnl == pytest.approx(100.0)  # 标的币种：10×(110−100)
    assert row.fx_pnl == pytest.approx(440.0)  # 报告币种：10×110×(7.4−7.0)
    assert row.total_report == pytest.approx(1_140.0)


def test_a_usd_position_without_a_rate_contributes_nothing(db, portfolio) -> None:
    """换不出报告币种时不给数字：把 1000 美元当成 1000 人民币加进总市值，
    是一个看起来完全正常的错数。
    """
    account_id = _us_position(portfolio, fx_rate_ref=7.0)
    BarRepo(db).upsert(_bars("US", "AAPL", [(DAY, 110.0)]))
    row = portfolio.positions(USER, account_id)[0]
    assert row.last_price == pytest.approx(110.0)
    assert row.pnl is None
    assert row.total_report == 0.0
    assert row.convertible is False
    assert "汇率" in row.unavailable_note


def test_a_position_missing_its_base_rate_is_not_split(db, portfolio) -> None:
    """有一笔买入不知道下单日的基准汇率，整段拆分就不成立：宁可不拆，也不猜。"""
    account_id = _us_position(portfolio)
    FxRepo(db).put(DAY, "USD", "CNY", 7.4, source="manual")
    BarRepo(db).upsert(_bars("US", "AAPL", [(DAY, 110.0)]))
    row = portfolio.positions(USER, account_id)[0]
    assert row.pnl is None
    assert "基准" in row.unavailable_note


def test_an_unknown_account_is_refused(db, portfolio) -> None:
    """读也带 user_id：不提供「查全量再由调用方过滤」的接口，隔离不该指望每个调用方都记得。"""
    with pytest.raises(ValidationError, match="不存在"):
        portfolio.positions(USER, "no-such-account")
    with pytest.raises(ValidationError, match="不存在"):
        portfolio.summary(USER, "no-such-account")


def _buy(portfolio: PortfolioService, account_id: str, **over) -> None:
    args = {
        "account_id": account_id,
        "user_id": USER,
        "market": "A",
        "code": "601088",
        "side": "buy",
        "qty": 100,
        "price": 40.0,
        "currency": "CNY",
        "fee": 0.0,
    } | over
    portfolio.orders.record_fill(**args)


def _sell(portfolio: PortfolioService, account_id: str, **over) -> None:
    args = {
        "account_id": account_id,
        "user_id": USER,
        "market": "A",
        "code": "601088",
        "side": "sell",
        "qty": 50,
        "price": 44.0,
        "currency": "CNY",
        "fee": 0.0,
    } | over
    portfolio.orders.record_fill(**args)


def test_the_summary_keeps_per_currency_subtotals_apart(db, portfolio) -> None:
    """把所有币种的数额直接相加，是「看起来完全正常」的那种错。"""
    account_id = _funded_position(portfolio, price=40.0)
    portfolio.accounts.adjust_cash(account_id, "USD", 1_000.0, reason="美元口袋", actor_id=USER)
    BarRepo(db).upsert(_bars("A", "601088", [(DAY, 44.0)]))
    summary = portfolio.summary(USER, account_id)
    # 1_000_000 注资 − 100×40 买入（helper 不留手续费）
    assert summary.cash_by_currency["CNY"] == pytest.approx(996_000.0)
    assert summary.cash_by_currency["USD"] == pytest.approx(1_000.0)
    assert summary.position_value_by_currency == {"CNY": pytest.approx(4_400.0)}


def test_the_report_currency_total_converts_each_piece(db, portfolio) -> None:
    account_id = _funded_position(portfolio, price=40.0)
    portfolio.accounts.adjust_cash(account_id, "USD", 1_000.0, reason="美元口袋", actor_id=USER)
    FxRepo(db).put(DAY, "USD", "CNY", 7.0, source="manual")
    BarRepo(db).upsert(_bars("A", "601088", [(DAY, 44.0)]))
    summary = portfolio.summary(USER, account_id)
    # 持仓 4400（CNY）+ 现金 996000 + 1000 USD 换 7000
    assert summary.total_report == pytest.approx(4_400.0 + 996_000.0 + 7_000.0)


def test_an_unconvertable_piece_is_named_and_left_out(db, portfolio) -> None:
    """没有 USD→CNY 汇率时，宁可少算一笔并在 notes 里点名。"""
    account_id = _funded_position(portfolio, price=40.0)
    portfolio.accounts.adjust_cash(account_id, "USD", 1_000.0, reason="美元口袋", actor_id=USER)
    BarRepo(db).upsert(_bars("A", "601088", [(DAY, 44.0)]))
    summary = portfolio.summary(USER, account_id)
    assert any("USD" in note for note in summary.notes), summary.notes
    assert summary.total_report == pytest.approx(4_400.0 + 996_000.0)


def test_a_position_without_a_price_is_excluded_from_the_subtotal(db, portfolio) -> None:
    """没有行情的那一行不顶一个「等于成本」的市值：那是把「今天没数」显示成「今天持平」。"""
    account_id = _funded_position(portfolio, price=40.0)
    summary = portfolio.summary(USER, account_id)
    assert summary.position_value_by_currency == {}
    assert summary.price_pnl_report == 0.0
    assert any("601088" in note for note in summary.notes)
    assert summary.total_report == pytest.approx(996_000.0)


def test_the_fx_piece_is_reported_on_its_own_line(db, portfolio) -> None:
    """换汇影响并进价格盈亏里，用户会把「人民币贬值」读成「这只票涨了」。"""
    account_id = _us_position(portfolio, fx_rate_ref=7.0)
    FxRepo(db).put(date(2024, 10, 1), "USD", "CNY", 7.0, source="manual")
    FxRepo(db).put(DAY, "USD", "CNY", 7.4, source="manual")
    BarRepo(db).upsert(_bars("US", "AAPL", [(DAY, 100.0)]))
    summary = portfolio.summary(USER, account_id)
    assert summary.fx_pnl == pytest.approx(10 * 100.0 * (7.4 - 7.0))
    assert summary.price_pnl_report == pytest.approx(0.0)
    assert summary.price_pnl_report + summary.fx_pnl == pytest.approx(summary.total_pnl_report)


def test_realized_pnl_is_its_own_line_and_not_part_of_the_position_change(db, portfolio) -> None:
    """已实现的是过去那一笔，未实现的是剩下的 50 股：混在一起就说不清「这笔交易赚了多少」。"""
    account_id = _funded_position(portfolio, price=40.0)
    _sell(portfolio, account_id)
    BarRepo(db).upsert(_bars("A", "601088", [(DAY, 44.0)]))
    summary = portfolio.summary(USER, account_id)
    assert summary.realized_report == pytest.approx(50 * (44.0 - 40.0))
    assert summary.price_pnl_report == pytest.approx(50 * (44.0 - 40.0))


def test_a_cash_only_account_anchors_the_conversion_to_the_clock(db, portfolio) -> None:
    """没有持仓就没有「行情日」，这时才问钟：锚的是注入的那一天，不是墙钟。"""
    account = portfolio.accounts.open(USER, "只有现金", report_currency="CNY")
    portfolio.accounts.adjust_cash(account.id, "USD", 1_000.0, reason="初始", actor_id=USER)
    FxRepo(db).put(TODAY, "USD", "CNY", 7.0, source="manual")
    summary = portfolio.summary(USER, account.id)
    assert summary.total_report == pytest.approx(7_000.0)


def test_a_position_held_in_two_accounts_is_called_out(db, portfolio) -> None:
    """库里不冲突（主键带 account_id），但读的人会以为是数据坏了。"""
    first = _funded_position(portfolio)
    second = portfolio.accounts.open(USER, "副账户", report_currency="CNY").id
    portfolio.accounts.adjust_cash(second, "CNY", 100_000.0, reason="初始", actor_id=USER)
    _buy(portfolio, second, qty=100, price=40.0)
    summary = portfolio.summary(USER, first)
    assert any("副账户" in line for line in summary.duplicate_holdings), summary.duplicate_holdings


@pytest.mark.parametrize("price", [40.0, 44.0, 50.0])
def test_price_pnl_plus_fx_always_equals_the_reported_change(db, portfolio, price) -> None:
    """§5.4 line 434 在读用例这层再证一次：T-2 证的是函数，这里证的是装配。"""
    account_id = _funded_position(portfolio, price=40.0)
    BarRepo(db).upsert(_bars("A", "601088", [(DAY, price)]))
    summary = portfolio.summary(USER, account_id)
    rows = [r for r in portfolio.positions(USER, account_id) if r.convertible]
    assert summary.price_pnl_report + summary.fx_pnl == pytest.approx(
        sum(r.total_report for r in rows)
    )
