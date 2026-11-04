"""每票日价值（S5-A §2.1）：归因用的「这天这只票值多少」由净值重建那一趟写下。

只记录、不重算：净值与归因必须出自同一趟取价。归因自己去 `bisect` 一遍 bars，两边就会在
「陈旧价怎么挡」「汇率取哪一天」上各自演化，最后出现「贡献加起来不等于净值涨幅」这种没法解释的表。

清仓那天要留一行（`qty=0`、价与汇率照写）：卖出当天的价格效应如果没有归属，就会全部掉进残差。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from sams.services.ledger import LedgerService
from sams.services.nav import NavService
from sams.storage.db import Database
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.corp_actions import CorpActionRepo
from sams.storage.repos.fx import FxRepo
from sams.storage.repos.holding_nav import HoldingNavRepo
from sams.storage.repos.ledger import SimAccountRepo, SimOrderRepo
from sams.storage.repos.symbols import SymbolRepo
from sams.storage.repos.theses import ThesisRepo
from sams.storage.repos.users import UserRepo

USER = "trader"
PWD = "correct-horse-battery"
SH = ZoneInfo("Asia/Shanghai")
OPENED_AT = datetime(2024, 1, 2, 1, 0, tzinfo=UTC)
START = date(2024, 1, 3)
END = date(2024, 1, 5)


def _at(day: date, hour: int = 15) -> datetime:
    return datetime(day.year, day.month, day.day, hour, 0, tzinfo=SH)


@pytest.fixture
def world(db: Database, settings):
    users = UserRepo(db)
    users.create(USER, PWD)
    accounts = SimAccountRepo(db)
    orders = SimOrderRepo(db, accounts, markets=settings.markets, symbols=SymbolRepo(db))
    ledger = LedgerService(accounts, orders, ThesisRepo(db), users, settings)
    bars = BarRepo(db)
    fx = FxRepo(db)
    nav = NavService(
        accounts, orders, bars, fx, CorpActionRepo(db), None, settings
    )
    account = ledger.open_account(USER, "主账户")
    db.execute("UPDATE sim_accounts SET created_at = ? WHERE id = ?", [OPENED_AT, account.id])
    accounts.adjust_cash(
        account.id,
        "CNY",
        100_000.0,
        reason="存入 初始",
        actor_id=USER,
        ts=_at(date(2024, 1, 2)),
        kind="deposit",
    )
    return SimpleNamespace(
        db=db,
        ledger=ledger,
        accounts=accounts,
        bars=bars,
        fx=fx,
        nav=nav,
        holdings=HoldingNavRepo(db),
        account=account,
    )


def _bars(world, closes: dict[date, float], *, market: str = "A", code: str = "601088") -> None:
    world.bars.upsert(
        pl.DataFrame(
            {
                "market": [market] * len(closes),
                "code": [code] * len(closes),
                "date": list(closes),
                "close": list(closes.values()),
                "source": ["test"] * len(closes),
            }
        )
    )


def _buy(world, day: date, *, market: str = "A", code: str = "601088", qty: float = 100,
         price: float = 10.0, fx_rate: float | None = None) -> None:
    world.ledger.buy(
        world.account.id, USER, market, code, qty=qty, price=price, fx_rate=fx_rate, ts=_at(day)
    )


def _sell(world, day: date, *, code: str = "601088", qty: float = 100, price: float = 10.0) -> None:
    world.ledger.sell(
        world.account.id, USER, "A", code, qty=qty, price=price, ts=_at(day)
    )


def _rows(world) -> list:
    return world.holdings.series(world.account.id, START, END)


def test_a_complete_day_records_what_each_holding_was_worth(world) -> None:
    _buy(world, START)
    _bars(world, {START: 10.0, date(2024, 1, 4): 11.0, END: 12.0})

    world.nav.rebuild(world.account.id, START, END)
    rows = _rows(world)

    assert [(row.day, row.market, row.code) for row in rows] == [
        (START, "A", "601088"),
        (date(2024, 1, 4), "A", "601088"),
        (END, "A", "601088"),
    ]
    middle = rows[1]
    assert middle.qty == pytest.approx(100.0)
    assert middle.price == pytest.approx(11.0)
    assert middle.fx_rate == pytest.approx(1.0)
    assert middle.value_report == pytest.approx(1_100.0)


def test_the_selling_day_keeps_a_row_so_its_price_move_has_an_owner(world) -> None:
    """卖出那天留一行 `qty=0`：这一段涨跌没有归属就会整段掉进归因残差。

    只延续一天：01-05 已经不是「昨天刚清仓」，那只票跟这个账户再无关系。
    """
    _buy(world, START, price=10.0)
    _bars(world, {START: 10.0, date(2024, 1, 4): 11.0, END: 12.0})
    _sell(world, date(2024, 1, 4), price=11.0)

    world.nav.rebuild(world.account.id, START, END)
    rows = _rows(world)

    assert [(row.day, row.qty) for row in rows] == [(START, 100.0), (date(2024, 1, 4), 0.0)]
    sold = rows[-1]
    assert sold.price == pytest.approx(11.0)
    assert sold.value_report == pytest.approx(0.0)


def test_rebuilding_the_same_range_does_not_stack_two_versions_of_history(world) -> None:
    _buy(world, START)
    _bars(world, {START: 10.0, date(2024, 1, 4): 11.0, END: 12.0})

    world.nav.rebuild(world.account.id, START, END)
    world.nav.rebuild(world.account.id, START, END)

    assert len(_rows(world)) == 3


def test_a_day_that_cannot_be_priced_records_no_holdings_at_all(world) -> None:
    """取不到价的那天连每票行都不写：净值行已经是 `complete=false`，归因只用完整日。

    注意「当天没有新收盘」不算取不到价——5 个交易日内沿用最后一根是净值本来的口径
    （`STALE_TRADING_DAYS`），归因跟着同一套取价，不该更严也不该更松。
    """
    _buy(world, START)
    _bars(world, {END: 12.0})  # START 与 01-04 之前根本没有收盘

    world.nav.rebuild(world.account.id, START, END)
    rows = _rows(world)

    assert [row.day for row in rows] == [END]


def test_a_hong_kong_holding_is_stored_both_as_its_own_price_and_as_report_value(world) -> None:
    """`price` 是本币收盘、`value_report` 是折过汇率的那个数：归因要按这两段各算一次。"""
    SymbolRepo(world.db).upsert(
        pl.DataFrame(
            {
                "market": ["HK"],
                "code": ["00700"],
                "name": ["腾讯控股"],
                "currency": ["HKD"],
                "sector": ["互联网"],
            }
        )
    )
    world.bars.upsert(
        pl.DataFrame(
            {
                "market": ["HK"] * 3,
                "code": ["00700"] * 3,
                "date": [START, date(2024, 1, 4), END],
                "close": [300.0, 310.0, 320.0],
                "source": ["test"] * 3,
            }
        )
    )
    world.fx.put(START, "HKD", "CNY", 0.90)
    world.fx.put(date(2024, 1, 4), "HKD", "CNY", 0.92)
    world.fx.put(END, "HKD", "CNY", 0.88)
    _buy(world, START, market="HK", code="00700", qty=200, price=300.0, fx_rate=0.90)

    world.nav.rebuild(world.account.id, START, END)
    rows = _rows(world)

    assert [(row.price, row.fx_rate) for row in rows] == [
        (300.0, 0.90),
        (310.0, 0.92),
        (320.0, 0.88),
    ]
    assert rows[1].value_report == pytest.approx(200 * 310.0 * 0.92)
