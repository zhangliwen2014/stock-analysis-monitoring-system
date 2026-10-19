"""读用例：持仓的现价、盈亏、以及它们与汇率的关系（§5.4、D73）。"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from sams.services.portfolio import PortfolioService
from sams.storage.db import Database
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.fx import FxRepo
from sams.storage.repos.ledger import SimAccountRepo, SimOrderRepo
from sams.storage.repos.symbols import SymbolRepo

USER = "u1"
DAY = date(2024, 10, 25)


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
    return PortfolioService(accounts, orders, BarRepo(db), FxRepo(db))


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
    with pytest.raises(Exception, match="不存在"):
        portfolio.positions(USER, "no-such-account")
