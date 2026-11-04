"""组合限额（S4-E §7）：自定义线才判违规，默认线只是参考，算不出就说算不出。

这一层最容易骗人的三处：
* 把默认参考线当成红线：20% 的单票在 25% 时标红，用户会以为系统在替自己设线；
* 数据不全时给 0%：缺价/缺行业的持仓没进分母，屏幕上却写着「正常」；
* 用区间最大回撤冒充「当前回撤」：早已修复的那一段会让今天误报超限。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import polars as pl
import pytest
import support

from sams.core.errors import ValidationError
from sams.services.industry import IndustryService
from sams.services.ledger import LedgerService
from sams.services.limits import (
    BREACH,
    OK,
    REFERENCE,
    UNKNOWN,
    LimitService,
)
from sams.services.portfolio import PortfolioService
from sams.storage.db import Database
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.f10 import CompanyProfileRepo
from sams.storage.repos.fx import FxRepo
from sams.storage.repos.ledger import SimAccountRepo, SimOrderRepo
from sams.storage.repos.limits import LimitRepo
from sams.storage.repos.symbols import SymbolRepo
from sams.storage.repos.theses import ThesisRepo
from sams.storage.repos.users import UserRepo

USER = "trader"
PWD = "correct-horse-battery"
SH = ZoneInfo("Asia/Shanghai")
TODAY = date(2024, 10, 26)


class FakeNav:
    """只答限额用得到的那三个数：当前回撤、缺口天数、有没有一行完整日。"""

    def __init__(self, drawdown: float | None = 0.0, gaps: int = 0, latest: bool = True):
        self.drawdown = drawdown
        self.gaps = gaps
        self.latest = latest
        self.calls: list[tuple[str, date, date]] = []

    def series(self, account_id: str, start: date, end: date):
        self.calls.append((account_id, start, end))
        return SimpleNamespace(
            latest=SimpleNamespace(day=end, nav=100_000.0) if self.latest else None,
            current_drawdown=self.drawdown if self.latest else None,
            gap_days=self.gaps,
        )


def _at(day: date, hour: int = 15) -> datetime:
    return datetime(day.year, day.month, day.day, hour, 0, tzinfo=SH)


def _symbols(db: Database, rows: list[tuple[str, str, str, str]]) -> SymbolRepo:
    symbols = SymbolRepo(db)
    symbols.upsert(
        pl.DataFrame(
            {
                "market": ["A"] * len(rows),
                "code": [code for code, _name, _sector, _ccy in rows],
                "name": [name for _code, name, _sector, _ccy in rows],
                "currency": [ccy for _code, _name, _sector, ccy in rows],
                "sector": [sector for _code, _name, sector, _ccy in rows],
            }
        )
    )
    return symbols


def _bars(db: Database, closes: dict[str, float]) -> None:
    BarRepo(db).upsert(
        pl.DataFrame(
            {
                "market": ["A"] * len(closes),
                "code": list(closes),
                "date": [date(2024, 10, 25)] * len(closes),
                "close": list(closes.values()),
                "source": ["test"] * len(closes),
            }
        )
    )


@pytest.fixture
def world(db: Database, settings):
    users = UserRepo(db)
    users.create(USER, PWD)
    symbols = _symbols(
        db,
        [
            ("601088", "中国神华", "煤炭", "CNY"),
            ("600519", "贵州茅台", "白酒", "CNY"),
        ],
    )
    accounts = SimAccountRepo(db)
    orders = SimOrderRepo(db, accounts, markets=settings.markets, symbols=symbols)
    ledger = LedgerService(accounts, orders, ThesisRepo(db), users, settings)
    bars = BarRepo(db)
    _bars(db, {"601088": 10.0, "600519": 20.0})
    fx = FxRepo(db)
    clock = support.FrozenClock(datetime(2024, 10, 26, 15, tzinfo=UTC))
    portfolio = PortfolioService(accounts, orders, bars, fx, users, clock=clock)
    profiles = CompanyProfileRepo(db)
    industry = IndustryService(profiles, symbols)
    nav = FakeNav()
    limits = LimitRepo(db, clock=clock)
    service = LimitService(
        limits,
        users,
        accounts,
        portfolio,
        industry,
        nav,
        symbols,
        settings,
        clock=clock,
    )
    account = ledger.open_account(USER, "主账户")
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
        settings=settings,
        users=users,
        symbols=symbols,
        accounts=accounts,
        orders=orders,
        ledger=ledger,
        bars=bars,
        fx=fx,
        profiles=profiles,
        industry=industry,
        nav=nav,
        limits=limits,
        service=service,
        account=account,
        clock=clock,
    )


def _buy(world, code: str, qty: float, price: float, day: date = date(2024, 6, 3)) -> None:
    world.ledger.buy(
        world.account.id, USER, "A", code, qty=qty, price=price, ts=_at(day)
    )


def _two_holdings(world) -> None:
    """4 万 + 1 万，现金 5 万：总资产 10 万，权重 40% / 10%（单票 40% 已经高于默认 20%）。"""
    _buy(world, "601088", 4_000, 10.0)
    _buy(world, "600519", 500, 20.0)


def _row(report, kind: str, scope: str | None = None):
    return next(
        row for row in report.rows if row.kind == kind and row.scope == (scope or "")
    )


def _save(world, kind: str, scope: str, threshold: float, **kwargs):
    return world.service.save(
        USER, kind=kind, scope=scope, threshold=threshold, **kwargs
    )


def test_a_custom_single_line_breaches_when_the_weight_crosses_it(world) -> None:
    _two_holdings(world)
    _save(world, "single", "601088", 0.30)

    report = world.service.evaluate(USER, world.account.id)
    row = _row(report, "single", "601088")

    assert row.status == BREACH
    assert row.actual == pytest.approx(0.40)
    assert report.breaches == (row,)


def test_a_line_that_stays_under_the_threshold_is_ok(world) -> None:
    _two_holdings(world)
    _save(world, "single", "601088", 0.50)

    row = _row(world.service.evaluate(USER, world.account.id), "single", "601088")

    assert row.status == OK
    assert row.actual == pytest.approx(0.40)


def test_the_default_single_line_is_a_reference_and_not_a_violation(world) -> None:
    """40% 的单票高于默认 20%：没有自定义线时它只是灰色参考，不算违规。"""
    _two_holdings(world)

    report = world.service.evaluate(USER, world.account.id)
    row = _row(report, "single", "601088")

    assert row.status == REFERENCE
    assert row.threshold == pytest.approx(0.20)
    assert report.breaches == ()


def test_the_reference_lines_cover_single_industry_and_drawdown(world) -> None:
    _two_holdings(world)

    report = world.service.evaluate(USER, world.account.id)

    assert {row.kind for row in report.rows} == {"single", "industry", "drawdown"}
    assert all(row.status == REFERENCE for row in report.rows)
    assert _row(report, "industry", "煤炭").actual == pytest.approx(0.40)
    assert _row(report, "drawdown").threshold == pytest.approx(0.20)


def test_a_custom_line_replaces_the_reference_of_its_kind_only(world) -> None:
    _two_holdings(world)
    _save(world, "industry", "煤炭", 0.30)

    report = world.service.evaluate(USER, world.account.id)

    assert _row(report, "industry", "煤炭").status == BREACH
    assert _row(report, "single", "601088").status == REFERENCE
    assert _row(report, "drawdown").status == REFERENCE


def test_a_market_line_uses_the_market_weight(world) -> None:
    _two_holdings(world)
    _save(world, "market", "A", 0.40)

    row = _row(world.service.evaluate(USER, world.account.id), "market", "A")

    assert row.actual == pytest.approx(0.50)
    assert row.status == BREACH


def test_an_industry_with_no_holding_is_zero_not_unknown(world) -> None:
    _two_holdings(world)
    _save(world, "industry", "银行", 0.30)

    row = _row(world.service.evaluate(USER, world.account.id), "industry", "银行")

    assert row.actual == 0.0
    assert row.status == OK


def test_an_industry_line_is_undecidable_when_a_holding_has_no_industry(world) -> None:
    _two_holdings(world)
    world.symbols.upsert(
        pl.DataFrame(
            {
                "market": ["A"],
                "code": ["601088"],
                "name": ["中国神华"],
                "currency": ["CNY"],
                "sector": [""],
            }
        )
    )
    _save(world, "industry", "煤炭", 0.30)

    row = _row(world.service.evaluate(USER, world.account.id), "industry", "煤炭")

    assert row.status == UNKNOWN
    assert row.actual is None
    assert "缺行业" in (row.note or "")


def test_an_unpriced_holding_makes_weight_lines_undecidable(world) -> None:
    _two_holdings(world)
    # 一只没有日线的票：它不进市值，分母跟着变小，40% 会变成一个偏低的数
    world.symbols.upsert(
        pl.DataFrame(
            {
                "market": ["A"],
                "code": ["000001"],
                "name": ["平安银行"],
                "currency": ["CNY"],
                "sector": ["银行"],
            }
        )
    )
    _buy(world, "000001", 100, 10.0)
    _save(world, "single", "601088", 0.30)

    row = _row(world.service.evaluate(USER, world.account.id), "single", "601088")

    assert row.status == UNKNOWN
    assert row.actual is None
    assert "缺价" in (row.note or "")


def test_the_drawdown_line_is_the_current_drawdown_not_the_window_max(world) -> None:
    _two_holdings(world)
    world.nav.drawdown = 0.25
    _save(world, "drawdown", "", 0.20)

    row = _row(world.service.evaluate(USER, world.account.id), "drawdown")

    assert row.actual == pytest.approx(0.25)
    assert row.status == BREACH


def test_a_nav_gap_makes_the_drawdown_line_undecidable(world) -> None:
    _two_holdings(world)
    world.nav.drawdown = 0.05
    world.nav.gaps = 2
    _save(world, "drawdown", "", 0.20)

    row = _row(world.service.evaluate(USER, world.account.id), "drawdown")

    assert row.status == UNKNOWN
    assert row.actual is None
    assert "缺口" in (row.note or "")


def test_a_drawdown_line_without_any_nav_says_how_to_build_it(world) -> None:
    _two_holdings(world)
    world.nav.latest = False

    row = _row(world.service.evaluate(USER, world.account.id), "drawdown")

    assert row.status == UNKNOWN
    assert "sams nav rebuild" in (row.note or "")
    # 那句话要能直接照做：说出盘后 job 每晚重建（不是「没人跑命令」），并带上真的账户 id
    # ——`<账户>` 占位符等于让每个人自己回去查那个 id（与归因卡同一句话，D281）
    assert "盘后" in (row.note or "")
    assert world.account.id in (row.note or "")


def test_the_nav_window_matches_the_rebuild_window(world) -> None:
    _two_holdings(world)

    world.service.evaluate(USER, world.account.id)

    _account, start, end = world.nav.calls[0]
    assert end == TODAY
    assert (end - start).days == 400


def test_saving_the_same_scope_updates_the_threshold_instead_of_duplicating(world) -> None:
    _two_holdings(world)
    first = _save(world, "single", "601088", 0.30)
    second = _save(world, "single", "601088", 0.50)

    assert second.id == first.id
    assert world.limits.list_for(world.users.get_by_username(USER).id) == (second,)


def test_a_threshold_must_be_a_ratio_between_zero_and_one(world) -> None:
    with pytest.raises(ValidationError, match="0 与 1"):
        _save(world, "single", "601088", 1.5)


def test_an_unknown_kind_is_refused(world) -> None:
    with pytest.raises(ValidationError, match="单票"):
        _save(world, "position", "601088", 0.3)


def test_a_weight_line_needs_a_scope_and_a_drawdown_line_does_not(world) -> None:
    with pytest.raises(ValidationError, match="对象"):
        _save(world, "single", "", 0.3)
    with pytest.raises(ValidationError, match="不要填对象"):
        _save(world, "drawdown", "601088", 0.2)
    _save(world, "drawdown", "", 0.2)


def test_a_stock_line_is_case_insensitive_and_normalized(world) -> None:
    """美股代码大小写是同一个代码：存小写的红线不能永远匹配不上。"""
    world.ledger.open_account(USER, "美账户", home_market="US")
    saved = _save(world, "single", "aapl", 0.3)

    assert saved.scope == "AAPL"


def test_breach_lines_name_the_account_and_the_scope(world) -> None:
    _two_holdings(world)
    _save(world, "single", "601088", 0.30)
    _save(world, "industry", "煤炭", 0.30)
    world.nav.drawdown = 0.30
    _save(world, "drawdown", "", 0.20)

    lines = world.service.breach_lines(USER, "A")

    assert any("主账户" in line and "601088" in line and "40.0%" in line for line in lines)
    assert any("行业「煤炭」" in line for line in lines)
    assert any("回撤" in line and "30.0%" in line for line in lines)


def test_breach_lines_for_another_market_are_not_in_this_digest(world) -> None:
    _two_holdings(world)
    _save(world, "single", "601088", 0.30)

    assert world.service.breach_lines(USER, "HK") == ()


def _add_hk_holding(world) -> None:
    """A 股账户里的一笔港股：5.4 万 / 10 万 = 54%。"""
    world.symbols.upsert(
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
                "market": ["HK"],
                "code": ["00700"],
                "date": [date(2024, 10, 25)],
                "close": [300.0],
                "source": ["test"],
            }
        )
    )
    world.fx.put(date(2024, 10, 25), "HKD", "CNY", 0.9)
    world.ledger.buy(
        world.account.id, USER, "HK", "00700", qty=200, price=300.0, fx_rate=0.9,
        ts=_at(date(2024, 6, 3)),
    )


def test_a_cross_market_holding_is_still_reported_to_its_own_account_digest(world) -> None:
    """摘要按账户归，不按标的归：A 股账户里的港股超标了要说给这份 A 摘要。

    标的自己属于哪个市场决定它进哪份摘要，会让只收 A 摘要的用户永远看不到自己
    账户里那条 HK 持仓的红线——它不是被过滤了，是根本没送到眼前。
    """
    _add_hk_holding(world)
    _save(world, "single", "00700", 0.30)

    lines = world.service.breach_lines(USER, "A")

    assert any("主账户" in line and "00700" in line for line in lines)
    assert world.service.breach_lines(USER, "HK") == ()


def test_a_limit_can_be_scoped_to_one_account(world) -> None:
    second = world.ledger.open_account(USER, "备用户")
    world.accounts.adjust_cash(
        second.id,
        "CNY",
        100_000.0,
        reason="存入 初始",
        actor_id=USER,
        ts=_at(date(2024, 1, 2)),
        kind="deposit",
    )
    _two_holdings(world)
    _save(world, "single", "601088", 0.30, account_id=second.id)

    # 限在备用户上：主账户这一屏不该出现这条线，备用户空仓时它是 0% 正常
    assert not any(
        row.kind == "single" and row.custom
        for row in world.service.evaluate(USER, world.account.id).rows
    )
    row = _row(world.service.evaluate(USER, second.id), "single", "601088")
    assert row.actual == 0.0
    assert row.status == OK


def test_deleting_a_line_brings_back_the_reference(world) -> None:
    _two_holdings(world)
    saved = _save(world, "single", "601088", 0.30)

    assert world.service.remove(USER, saved.id) is True

    report = world.service.evaluate(USER, world.account.id)
    assert _row(report, "single", "601088").status == REFERENCE
    assert world.service.remove(USER, saved.id) is False


def test_another_users_line_is_not_theirs_to_delete(world) -> None:
    world.users.create("bob", PWD)
    saved = _save(world, "single", "601088", 0.30)

    assert world.service.remove("bob", saved.id) is False
