"""组合净值（S4-D §6.3）：逐日重放、外部流入不计收益、缺口不拼、回撤按净值。

这个文件里最要紧的两条是「算不出来 ≠ 没变化」与「缺口不当 0 收益」：
缺价/缺汇率的那一天写 `complete=false`、`nav=NULL`，TWR 只在连续完整日上累计，
缺口前后各算各的。用前值或 0 补出来的净值曲线看起来平滑，却会让一次停牌被读成一段零波动。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from sams.core.errors import StorageError
from sams.services.ledger import LedgerService
from sams.services.nav import NavService
from sams.storage.db import Database
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.corp_actions import CorpAction, CorpActionRepo
from sams.storage.repos.fx import FxRepo
from sams.storage.repos.ledger import SimAccountRepo, SimOrderRepo
from sams.storage.repos.symbols import SymbolRepo
from sams.storage.repos.theses import ThesisRepo
from sams.storage.repos.users import UserRepo

USER = "trader"
PWD = "correct-horse-battery"
SH = ZoneInfo("Asia/Shanghai")
OPENED_AT = datetime(2024, 1, 2, 1, 0, tzinfo=UTC)


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
    actions = CorpActionRepo(db)
    nav = NavService(accounts, orders, bars, fx, actions, None, settings)
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
        actions=actions,
        nav=nav,
        account=account,
        settings=settings,
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


def _buy(world, day: date, *, qty: float = 100, price: float = 10.0) -> None:
    world.ledger.buy(
        world.account.id, USER, "A", "601088", qty=qty, price=price, ts=_at(day)
    )


def _deposit(world, day: date, amount: float) -> None:
    world.accounts.adjust_cash(
        world.account.id,
        "CNY",
        amount,
        reason="存入 追加",
        actor_id=USER,
        ts=_at(day),
        kind="deposit",
    )


def _row(series, day: date):
    return next(point for point in series.days if point.day == day)


def test_daily_replay_marks_positions_to_the_close(world) -> None:
    _buy(world, date(2024, 1, 3))
    _bars(world, {date(2024, 1, 3): 10.0, date(2024, 1, 4): 11.0, date(2024, 1, 5): 12.0})

    written = world.nav.rebuild(world.account.id, date(2024, 1, 3), date(2024, 1, 5))
    series = world.nav.series(world.account.id, date(2024, 1, 3), date(2024, 1, 5))

    assert written == 3
    assert _row(series, date(2024, 1, 3)).nav == pytest.approx(100_000.0)
    assert _row(series, date(2024, 1, 4)).nav == pytest.approx(100_100.0)
    assert _row(series, date(2024, 1, 5)).nav == pytest.approx(100_200.0)
    assert series.latest is not None and series.latest.day == date(2024, 1, 5)
    assert series.twr == pytest.approx(100_200.0 / 100_000.0 - 1.0)


def test_an_external_deposit_is_not_counted_as_return(world) -> None:
    _deposit(world, date(2024, 1, 3), 10_000.0)

    world.nav.rebuild(world.account.id, date(2024, 1, 2), date(2024, 1, 3))
    series = world.nav.series(world.account.id, date(2024, 1, 2), date(2024, 1, 3))

    assert _row(series, date(2024, 1, 3)).nav == pytest.approx(110_000.0)
    assert _row(series, date(2024, 1, 3)).external_flow == pytest.approx(10_000.0)
    assert series.twr == pytest.approx(0.0)


def test_the_curve_starts_at_the_earliest_ledger_event_not_at_the_record(
    world,
) -> None:
    """R3-18：账是 1 月 10 日建的，补记的成交与存入在 1 月 2 日——曲线要从 1 月 2 日起（D294）。

    拿 `sim_accounts.created_at` 钳起点，等于让「先建账、再回填真实券商历史」这种用法
    永远没有开头那几天：那段的行情、除权日都落不进窗口，屏幕上只看到曲线比持仓短一截。
    """
    world.db.execute(
        "UPDATE sim_accounts SET created_at = ? WHERE id = ?",
        [datetime(2024, 1, 10, 1, 0, tzinfo=UTC), world.account.id],
    )
    _buy(world, date(2024, 1, 3))
    _bars(world, {date(2024, 1, 2): 10.0, date(2024, 1, 3): 10.0, date(2024, 1, 4): 11.0})

    written = world.nav.rebuild(world.account.id, date(2024, 1, 2), date(2024, 1, 4))
    series = world.nav.series(world.account.id, date(2024, 1, 2), date(2024, 1, 4))

    assert written == 3
    assert [point.day for point in series.days] == [
        date(2024, 1, 2),
        date(2024, 1, 3),
        date(2024, 1, 4),
    ]
    # 1 月 2 日只有那笔存入：还没买，净值就是现金
    assert _row(series, date(2024, 1, 2)).nav == pytest.approx(100_000.0)
    assert _row(series, date(2024, 1, 4)).nav == pytest.approx(100_100.0)


def test_the_earliest_ledger_event_still_respects_the_requested_window(world) -> None:
    """起点提前是按「台账里最早的那条」来的，不是无限往前：请求窗口更早就停在窗口头。"""
    _buy(world, date(2024, 1, 3))
    _bars(world, {date(2024, 1, 2): 10.0, date(2024, 1, 3): 10.0})

    written = world.nav.rebuild(world.account.id, date(2024, 1, 2), date(2024, 1, 3))

    assert written == 2


def test_a_missing_price_makes_the_whole_day_incomplete(world) -> None:
    _buy(world, date(2024, 1, 3))
    _bars(world, {date(2024, 1, 4): 11.0})

    world.nav.rebuild(world.account.id, date(2024, 1, 3), date(2024, 1, 4))
    series = world.nav.series(world.account.id, date(2024, 1, 3), date(2024, 1, 4))

    missing = _row(series, date(2024, 1, 3))
    assert missing.complete is False
    assert missing.nav is None
    assert missing.cash is None and missing.market_value is None
    assert missing.note and "601088" in missing.note
    assert _row(series, date(2024, 1, 4)).complete is True


def test_stale_prices_are_missing_after_more_than_five_trading_days(world) -> None:
    """最后收盘 01-04：到 01-11 是第 5 个交易日（还能用），到 01-12 是第 6 个（缺价）。"""
    _buy(world, date(2024, 1, 3))
    _bars(world, {date(2024, 1, 3): 10.0, date(2024, 1, 4): 11.0})

    world.nav.rebuild(world.account.id, date(2024, 1, 4), date(2024, 1, 12))
    series = world.nav.series(world.account.id, date(2024, 1, 4), date(2024, 1, 12))

    assert _row(series, date(2024, 1, 11)).complete is True
    stale = _row(series, date(2024, 1, 12))
    assert stale.complete is False
    assert stale.stale_prices >= 1
    assert stale.note and "超过" in stale.note


def test_twr_does_not_bridge_a_gap(world) -> None:
    _buy(world, date(2024, 1, 3))
    _bars(
        world,
        {
            date(2024, 1, 3): 10.0,
            date(2024, 1, 4): 11.0,
            date(2024, 1, 15): 12.0,
            date(2024, 1, 16): 13.0,
        },
    )

    world.nav.rebuild(world.account.id, date(2024, 1, 3), date(2024, 1, 16))
    series = world.nav.series(world.account.id, date(2024, 1, 3), date(2024, 1, 16))

    assert series.gap_days > 0
    assert series.twr_start == date(2024, 1, 15)
    assert series.twr == pytest.approx(100_300.0 / 100_200.0 - 1.0)


def test_max_drawdown_is_peak_to_trough_on_nav(world) -> None:
    _buy(world, date(2024, 1, 3))
    _bars(
        world,
        {
            date(2024, 1, 3): 10.0,
            date(2024, 1, 4): 12.0,
            date(2024, 1, 5): 9.0,
            date(2024, 1, 8): 11.0,
        },
    )

    world.nav.rebuild(world.account.id, date(2024, 1, 3), date(2024, 1, 8))
    series = world.nav.series(world.account.id, date(2024, 1, 3), date(2024, 1, 8))

    assert series.max_drawdown == pytest.approx((100_200.0 - 99_900.0) / 100_200.0)


def test_the_current_drawdown_is_peak_to_latest_not_the_window_max(world) -> None:
    """限额问的是「现在离高点多远」：早先那段更深的坑已经部分修复，不该拿来报超限。"""
    _buy(world, date(2024, 1, 3))
    _bars(
        world,
        {
            date(2024, 1, 3): 10.0,
            date(2024, 1, 4): 12.0,
            date(2024, 1, 5): 9.0,
            date(2024, 1, 8): 11.0,
        },
    )

    world.nav.rebuild(world.account.id, date(2024, 1, 3), date(2024, 1, 8))
    series = world.nav.series(world.account.id, date(2024, 1, 3), date(2024, 1, 8))

    assert series.max_drawdown == pytest.approx((100_200.0 - 99_900.0) / 100_200.0)
    assert series.current_drawdown == pytest.approx((100_200.0 - 100_100.0) / 100_200.0)


def test_a_hong_kong_position_is_converted_at_the_daily_rate(world) -> None:
    world.ledger.buy(
        world.account.id,
        USER,
        "HK",
        "00700",
        qty=200,
        price=300.0,
        fx_rate=0.9,
        ts=datetime(2024, 1, 4, 7, 0, tzinfo=UTC),
    )
    _bars(world, {date(2024, 1, 4): 310.0}, market="HK", code="00700")
    world.fx.put(date(2024, 1, 4), "HKD", "CNY", 0.9)

    world.nav.rebuild(world.account.id, date(2024, 1, 4), date(2024, 1, 4))
    series = world.nav.series(world.account.id, date(2024, 1, 4), date(2024, 1, 4))
    point = _row(series, date(2024, 1, 4))

    assert point.complete is True
    # 现金：100000 − 200×300×0.9 = 46000；持仓：200×310×0.9 = 55800
    assert point.nav == pytest.approx(46_000.0 + 55_800.0)


def test_a_missing_fx_rate_makes_the_day_incomplete(world) -> None:
    world.ledger.buy(
        world.account.id,
        USER,
        "HK",
        "00700",
        qty=200,
        price=300.0,
        fx_rate=0.9,
        ts=datetime(2024, 1, 4, 7, 0, tzinfo=UTC),
    )
    _bars(world, {date(2024, 1, 4): 310.0}, market="HK", code="00700")

    world.nav.rebuild(world.account.id, date(2024, 1, 4), date(2024, 1, 4))
    series = world.nav.series(world.account.id, date(2024, 1, 4), date(2024, 1, 4))
    point = _row(series, date(2024, 1, 4))

    assert point.complete is False
    assert point.nav is None
    assert point.note and "汇率" in point.note


def test_a_split_in_the_range_warns_without_changing_the_holdings(world) -> None:
    _buy(world, date(2024, 1, 3))
    _bars(world, {date(2024, 1, 3): 10.0, date(2024, 1, 4): 5.0})
    world.actions.upsert(
        CorpAction(
            market="A",
            code="601088",
            ex_date=date(2024, 1, 4),
            kind="split",
            src="test",
            ratio=2.0,
        )
    )

    world.nav.rebuild(world.account.id, date(2024, 1, 3), date(2024, 1, 4))
    series = world.nav.series(world.account.id, date(2024, 1, 3), date(2024, 1, 4))

    assert series.split_warning and "送转/拆股" in series.split_warning
    # 股数不调整：100 × 5，而不是 200 × 5（v0 不自动改用户台账）
    assert _row(series, date(2024, 1, 4)).nav == pytest.approx(99_000.0 + 500.0)


def test_rebuilding_the_same_range_is_idempotent(world) -> None:
    _buy(world, date(2024, 1, 3))
    _bars(world, {date(2024, 1, 3): 10.0, date(2024, 1, 4): 11.0})

    first = world.nav.rebuild(world.account.id, date(2024, 1, 3), date(2024, 1, 4))
    second = world.nav.rebuild(world.account.id, date(2024, 1, 3), date(2024, 1, 4))
    series = world.nav.series(world.account.id, date(2024, 1, 3), date(2024, 1, 4))

    assert first == second == 2
    assert len(series.days) == 2
    assert _row(series, date(2024, 1, 4)).nav == pytest.approx(100_100.0)


def test_the_series_never_starts_before_the_account_was_opened(world) -> None:
    _bars(world, {date(2024, 1, 3): 10.0})

    written = world.nav.rebuild(world.account.id, date(2023, 1, 1), date(2024, 1, 3))
    series = world.nav.series(world.account.id, date(2023, 1, 1), date(2024, 1, 3))

    assert written == 2  # 01-02 开户日与 01-03；2023 年一整年不写
    assert series.days[0].day == date(2024, 1, 2)


def test_an_unknown_account_is_refused(world) -> None:
    with pytest.raises(StorageError):
        world.nav.rebuild("not-an-account", date(2024, 1, 3), date(2024, 1, 4))
