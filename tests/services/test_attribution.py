"""业绩归因（S5-B §3）：这段时间赚的钱是谁带来的，加起来不等于总收益时差多少就说多少。

三条会让这张表骗人的口径：

* **买入当天不计收益**：新进的那笔钱不是这段行情的功劳，用它算涨跌会把建仓当成收益；
* **缺口不跨**：窗口只取连续完整日，实际用了哪几天写在报告上，不拿首尾两点连线冒充；
* **算不出就说算不出**：不足两个完整日时整张表是空的并给原因，而不是给一串 0。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from sams.core.errors import StorageError
from sams.services.attribution import AttributionService
from sams.services.industry import IndustryService
from sams.services.ledger import LedgerService
from sams.services.nav import NavService
from sams.storage.db import Database
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.corp_actions import CorpActionRepo
from sams.storage.repos.f10 import CompanyProfileRepo
from sams.storage.repos.fx import FxRepo
from sams.storage.repos.holding_nav import HoldingNavRepo
from sams.storage.repos.index_daily import IndexDailyRepo
from sams.storage.repos.ledger import SimAccountRepo, SimOrderRepo
from sams.storage.repos.nav import NavRepo
from sams.storage.repos.symbols import SymbolRepo
from sams.storage.repos.theses import ThesisRepo
from sams.storage.repos.users import UserRepo

USER = "trader"
PWD = "correct-horse-battery"
SH = ZoneInfo("Asia/Shanghai")
OPENED_AT = datetime(2024, 1, 2, 1, 0, tzinfo=UTC)
DAY_1 = date(2024, 1, 3)
DAY_2 = date(2024, 1, 4)
DAY_3 = date(2024, 1, 5)
DAY_4 = date(2024, 1, 8)


def _at(day: date, hour: int = 15) -> datetime:
    return datetime(day.year, day.month, day.day, hour, 0, tzinfo=SH)


@pytest.fixture
def world(db: Database, settings):
    users = UserRepo(db)
    users.create(USER, PWD)
    symbols = SymbolRepo(db)
    symbols.upsert(
        pl.DataFrame(
            {
                "market": ["A", "A"],
                "code": ["601088", "600519"],
                "name": ["中国神华", "贵州茅台"],
                "currency": ["CNY", "CNY"],
                "sector": ["煤炭", "白酒"],
            }
        )
    )
    accounts = SimAccountRepo(db)
    orders = SimOrderRepo(db, accounts, markets=settings.markets, symbols=symbols)
    ledger = LedgerService(accounts, orders, ThesisRepo(db), users, settings)
    bars = BarRepo(db)
    fx = FxRepo(db)
    nav = NavService(accounts, orders, bars, fx, CorpActionRepo(db), None, settings)
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
    attribution = AttributionService(
        users,
        accounts,
        NavRepo(db),
        HoldingNavRepo(db),
        IndustryService(CompanyProfileRepo(db), symbols),
        symbols,
        IndexDailyRepo(db),
        settings,
    )
    return SimpleNamespace(
        db=db,
        users=users,
        symbols=symbols,
        accounts=accounts,
        ledger=ledger,
        bars=bars,
        fx=fx,
        index=IndexDailyRepo(db),
        nav=nav,
        account=account,
        attribution=attribution,
    )


def _bars(world, code: str, closes: dict[date, float]) -> None:
    world.bars.upsert(
        pl.DataFrame(
            {
                "market": ["A"] * len(closes),
                "code": [code] * len(closes),
                "date": list(closes),
                "close": list(closes.values()),
                "source": ["test"] * len(closes),
            }
        )
    )


def _buy(world, code: str, qty: float, price: float, day: date = DAY_1) -> None:
    world.ledger.buy(world.account.id, USER, "A", code, qty=qty, price=price, ts=_at(day))


def _report(world):
    world.nav.rebuild(world.account.id, DAY_1, DAY_3)
    return world.attribution.evaluate(USER, world.account.id, DAY_1, DAY_3)


def _row(report, code: str):
    return next(row for row in report.rows if row.code == code)


def test_two_holdings_one_up_one_down_split_the_window_return(world) -> None:
    """40% 的煤炭涨 2 元、10% 的白酒跌 2 元：贡献分别 8% 与 −1%，其余是复利的未解释部分。"""
    _buy(world, "601088", 4_000, 10.0)
    _buy(world, "600519", 500, 20.0)
    _bars(world, "601088", {DAY_1: 10.0, DAY_2: 10.5, DAY_3: 12.0})
    _bars(world, "600519", {DAY_1: 20.0, DAY_2: 20.0, DAY_3: 18.0})

    report = _report(world)

    assert (report.start, report.end, report.days) == (DAY_1, DAY_3, 3)
    # DAY_2：煤炭 +0.5×4000/100000 = 2%（净值 100k→102k），白酒 0
    # DAY_3：煤炭 +1.5×4000/102000，白酒 −2×500/102000（净值 102k→107k）
    assert _row(report, "601088").contribution == pytest.approx(0.02 + 6_000.0 / 102_000.0)
    assert _row(report, "600519").contribution == pytest.approx(-1_000.0 / 102_000.0)
    # TWR 是逐日链乘，日收益直接相加会差一个复利交叉项——这个差要显式留在残差里
    total = sum(row.contribution for row in report.rows)
    assert report.window_return == pytest.approx(1.02 * (1 + 5_000.0 / 102_000.0) - 1)
    assert report.residual == pytest.approx(report.window_return - total)
    assert report.residual == pytest.approx(0.02 * (5_000.0 / 102_000.0))
    assert report.skipped == ""


def test_a_position_bought_mid_window_does_not_claim_the_day_it_appeared(world) -> None:
    """新进的那天不计收益：茅台 19→20 那段是建仓前的行情，这账户那天还没拿着它。"""
    _buy(world, "601088", 4_000, 10.0)
    _buy(world, "600519", 500, 20.0, day=DAY_2)
    _bars(world, "601088", {DAY_1: 10.0, DAY_2: 12.0, DAY_3: 11.0})
    _bars(world, "600519", {DAY_1: 19.0, DAY_2: 20.0, DAY_3: 22.0})

    report = _report(world)

    # 白酒只有 DAY_3 那段：500 × (22 − 20) / 108000，DAY_2 的 19→20 不算
    assert _row(report, "600519").contribution == pytest.approx(1_000.0 / 108_000.0)
    assert _row(report, "601088").contribution == pytest.approx(0.08 - 4_000.0 / 108_000.0)


def test_the_selling_day_still_earns_its_price_move(world) -> None:
    """卖出当天照计：10→12 那段是拿着它走完的，不因为收盘时已经清仓就抹掉。"""
    _buy(world, "601088", 4_000, 10.0)
    world.ledger.sell(
        world.account.id, USER, "A", "601088", qty=4_000, price=12.0, ts=_at(DAY_2)
    )
    _bars(world, "601088", {DAY_1: 10.0, DAY_2: 12.0, DAY_3: 11.0})

    report = _report(world)

    assert [row.code for row in report.rows] == ["601088"]
    assert _row(report, "601088").contribution == pytest.approx(0.08)
    assert report.window_return == pytest.approx(0.08)
    assert report.residual == pytest.approx(0.0, abs=1e-12)


def test_the_fx_move_is_kept_apart_from_the_price_move(world) -> None:
    """港股那段港币升值与那段股价上涨分开报：两段相加就是它的贡献。"""
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
                "market": ["HK"] * 3,
                "code": ["00700"] * 3,
                "date": [DAY_1, DAY_2, DAY_3],
                "close": [300.0, 300.0, 310.0],
                "source": ["test"] * 3,
            }
        )
    )
    for day, rate in [(DAY_1, 0.90), (DAY_2, 0.95), (DAY_3, 0.95)]:
        world.fx.put(day, "HKD", "CNY", rate)
    world.ledger.buy(
        world.account.id, USER, "HK", "00700", qty=200, price=300.0, fx_rate=0.90, ts=_at(DAY_1)
    )

    report = _report(world)
    row = _row(report, "00700")

    assert row.fx_part == pytest.approx(0.03)  # DAY_2 港币 0.90→0.95
    assert row.price_part == pytest.approx(1_900.0 / 103_000.0)  # DAY_3 300→310 HKD
    assert row.contribution == pytest.approx(row.price_part + row.fx_part)


def _dividend(world, code: str, day: date, amount: float, *, currency: str = "CNY") -> None:
    """照 `DividendService` 的样子写一笔派息入账：币种是标的币种，`ref` 带除权日。"""
    world.accounts.adjust_cash(
        world.account.id,
        currency,
        amount,
        reason=f"分红 {code} 除权 {day}（税前）",
        actor_id="system",
        ts=_at(day),
        kind="dividend",
        ref=f"A:{code}:{day}",
    )


def test_a_dividend_is_attributed_to_the_stock_that_paid_it(world) -> None:
    """股价没动、到账 2000 分红：这 2% 是那只票给的，不是说不清的残差。"""
    _buy(world, "601088", 4_000, 10.0)
    _bars(world, "601088", {DAY_1: 10.0, DAY_2: 10.0, DAY_3: 10.0})
    _dividend(world, "601088", DAY_2, 2_000.0)

    report = _report(world)
    row = _row(report, "601088")

    assert row.price_part == pytest.approx(0.0, abs=1e-12)
    assert row.fx_part == pytest.approx(0.0, abs=1e-12)
    assert row.dividend_part == pytest.approx(2_000.0 / 100_000.0)
    assert row.contribution == pytest.approx(row.price_part + row.fx_part + row.dividend_part)
    assert report.window_return == pytest.approx(0.02)
    assert report.residual == pytest.approx(0.0, abs=1e-12)


def test_a_holding_without_industry_lands_in_unclassified(world) -> None:
    """主档没行业就单独成一行「未分类」：摊进别的行业会让那几个行业的贡献看着更厚。"""
    world.symbols.upsert(
        pl.DataFrame(
            {
                "market": ["A"],
                "code": ["300750"],
                "name": ["宁德时代"],
                "currency": ["CNY"],
                "sector": [""],
            }
        )
    )
    _buy(world, "601088", 4_000, 10.0)
    _buy(world, "300750", 200, 50.0)
    _bars(world, "601088", {DAY_1: 10.0, DAY_2: 10.0, DAY_3: 10.0})
    _bars(world, "300750", {DAY_1: 50.0, DAY_2: 50.0, DAY_3: 55.0})

    report = _report(world)

    assert {group.label for group in report.industries} == {"煤炭", "未分类"}
    unclassified = next(g for g in report.industries if g.label == "未分类")
    assert unclassified.contribution == pytest.approx(1_000.0 / 100_000.0)


def _hk_only(world) -> None:
    """一笔港股 + 汇率从 DAY_3 才有：更早的日子取不到汇率，净值算不出来。"""
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
                "market": ["HK"] * 4,
                "code": ["00700"] * 4,
                "date": [DAY_1, DAY_2, DAY_3, DAY_4],
                "close": [300.0, 300.0, 300.0, 320.0],
                "source": ["test"] * 4,
            }
        )
    )
    world.fx.put(DAY_3, "HKD", "CNY", 0.90)
    world.fx.put(DAY_4, "HKD", "CNY", 0.90)
    world.ledger.buy(
        world.account.id, USER, "HK", "00700", qty=200, price=300.0, fx_rate=0.90, ts=_at(DAY_1)
    )


def test_a_gap_shrinks_the_window_and_says_how_much_was_dropped(world) -> None:
    """缺汇率的那几天不并入：窗口缩到能算的那一段，丢掉几天写在报告上。"""
    _hk_only(world)

    world.nav.rebuild(world.account.id, DAY_1, DAY_4)
    report = world.attribution.evaluate(USER, world.account.id, DAY_1, DAY_4)

    # 前两个完整日就是周五到周一那一段：周末不是缺口，用周五收盘价照常能算出来，
    # 只是那两天贡献 0——`days` 数的是日历天，不是交易日
    assert (report.start, report.end) == (DAY_3, DAY_4)
    assert (report.days, report.dropped_days) == (4, 2)
    # 分母是变动前一天的净值（周六的 100000），不是窗口起点、更不是期末
    assert _row(report, "00700").contribution == pytest.approx(3_600.0 / 100_000.0)
    assert report.window_return == pytest.approx(0.036)
    assert report.residual == pytest.approx(0.0, abs=1e-12)


def _index(world, closes: dict[date, float], *, code: str = "sh000001") -> None:
    world.index.upsert(
        pl.DataFrame(
            {
                "market": ["A"] * len(closes),
                "index_code": [code] * len(closes),
                "date": list(closes),
                "close": list(closes.values()),
                "source": ["test"] * len(closes),
            }
        )
    )


def _index_hk(world, closes: dict[date, float], *, code: str = "hkHSI") -> None:
    world.index.upsert(
        pl.DataFrame(
            {
                "market": ["HK"] * len(closes),
                "index_code": [code] * len(closes),
                "date": list(closes),
                "close": list(closes.values()),
                "source": ["test"] * len(closes),
            }
        )
    )


def _buy_hk(world, code: str, qty: float, price: float, day: date = DAY_1) -> None:
    """港股建仓：这个账户报告币种是 CNY，所以按 D209 从 CNY 口袋按汇率结算。"""
    world.symbols.upsert(
        pl.DataFrame(
            {
                "market": ["HK"],
                "code": [code],
                "name": ["腾讯"],
                "currency": ["HKD"],
                "sector": ["互联网"],
            }
        )
    )
    world.ledger.buy(
        world.account.id,
        USER,
        "HK",
        code,
        qty=qty,
        price=price,
        fx_rate=0.9,
        ts=_at(day),
    )
    for one in (DAY_1, DAY_2, DAY_3):
        world.fx.put(one, "HKD", "CNY", 0.9)


def _bars_hk(world, code: str, closes: dict[date, float]) -> None:
    world.bars.upsert(
        pl.DataFrame(
            {
                "market": ["HK"] * len(closes),
                "code": [code] * len(closes),
                "date": list(closes),
                "close": list(closes.values()),
                "source": ["test"] * len(closes),
            }
        )
    )


def test_the_benchmark_excess_uses_the_first_and_last_close_in_the_window(world) -> None:
    """指数涨了 5%、组合没动：超额 −5%，基准与起止日一起说清。"""
    _buy(world, "601088", 4_000, 10.0)
    _bars(world, "601088", {DAY_1: 10.0, DAY_2: 10.0, DAY_3: 10.0})
    _index(world, {DAY_1: 3_000.0, DAY_2: 3_100.0, DAY_3: 3_150.0})

    report = _report(world)

    assert report.benchmark.label == "上证指数"
    assert (report.benchmark.start, report.benchmark.end) == (DAY_1, DAY_3)
    assert report.benchmark.value == pytest.approx(0.05)
    assert report.window_return == pytest.approx(0.0)
    assert report.excess == pytest.approx(-0.05)


def test_no_index_close_in_the_window_gives_no_excess_instead_of_a_zero(world) -> None:
    """基准取不到时超额是「没有」，不是 0：0 会被读成「跑平大盘」，那是个假结论。"""
    _buy(world, "601088", 4_000, 10.0)
    _bars(world, "601088", {DAY_1: 10.0, DAY_2: 10.0, DAY_3: 10.0})

    report = _report(world)

    assert report.benchmark.value is None
    assert report.excess is None
    assert report.benchmark.note != ""


def test_a_cash_only_run_has_no_benchmark_to_compare_against(world) -> None:
    """整段都是现金：没有持仓就没有「这一段压在哪几个市场」，超额不评。"""
    report = _report(world)

    assert report.benchmark is not None
    assert report.benchmark.value is None
    assert report.excess is None
    assert "没有持仓" in report.benchmark.note


def test_a_benchmark_that_misses_a_day_in_the_window_gives_no_excess(world) -> None:
    """基准缺中间那一天：不给超额，也不「按现有的两根连乘」（D292）。

    一句 note 洗不掉两个市场的 β 之差，同样洗不掉一段没走完的行情——
    缺几天的收盘算出来的仍然是个「超额」，而它已经不代表这段时间。
    """
    _buy(world, "601088", 4_000, 10.0)
    _bars(world, "601088", {DAY_1: 10.0, DAY_2: 10.0, DAY_3: 10.0})
    _index(world, {DAY_1: 3_000.0, DAY_3: 3_150.0})

    report = _report(world)

    assert report.benchmark.value is None
    assert report.excess is None
    assert str(DAY_2) in report.benchmark.note
    assert "只出绝对收益" in report.benchmark.note
    # 组合这段的数照出：不给基准不等于不给收益
    assert report.window_return is not None


def test_a_benchmark_that_starts_or_ends_off_the_run_gives_no_excess(world) -> None:
    """基准的首根收盘比窗口起点晚一天：那一段行情的涨跌没被算进来，不给超额。"""
    _buy(world, "601088", 4_000, 10.0)
    _bars(world, "601088", {DAY_1: 10.0, DAY_2: 10.0, DAY_3: 10.0})
    _index(world, {DAY_2: 3_100.0, DAY_3: 3_150.0})

    report = _report(world)

    assert report.benchmark.value is None
    assert report.excess is None
    assert str(DAY_1) in report.benchmark.note


def test_an_all_hong_kong_run_is_measured_against_hong_kong(world) -> None:
    """R3-05：账户开在 A，这一段却全是港股——基准跟着持仓走（D291）。

    拿上证指数去比一个 HKD 仓位，那个「超额」是两个市场的 β 之差，不是判断的钱。
    """
    _buy_hk(world, "00700", 200, 300.0)
    _bars_hk(world, "00700", {DAY_1: 300.0, DAY_2: 300.0, DAY_3: 300.0})
    _index(world, {DAY_1: 3_000.0, DAY_2: 3_100.0, DAY_3: 3_150.0})
    _index_hk(world, {DAY_1: 20_000.0, DAY_2: 20_400.0, DAY_3: 20_600.0})

    report = _report(world)

    assert report.benchmark.market == "HK"
    assert report.benchmark.label == "恒生指数"
    # 恒指这段涨了 3%，组合没动：超额是 −3%，而不是拿 A 股那 5% 算出来的 −5%
    assert report.benchmark.value == pytest.approx(0.03)
    assert report.excess == pytest.approx(-0.03)
    assert "上证指数" not in report.benchmark.label


def test_a_run_spanning_two_markets_gets_no_benchmark_at_all(world) -> None:
    """A 与 HK 都有仓位：任何单一指数都不配当这段的基准，只出绝对收益（D291）。

    合成基准（A 70% + HK 30%）这轮不做：宁可不给那个数，也不给一个说不出算法的数。
    """
    _buy(world, "601088", 4_000, 10.0)
    _bars(world, "601088", {DAY_1: 10.0, DAY_2: 10.0, DAY_3: 10.0})
    _buy_hk(world, "00700", 200, 300.0)
    _bars_hk(world, "00700", {DAY_1: 300.0, DAY_2: 300.0, DAY_3: 300.0})
    _index(world, {DAY_1: 3_000.0, DAY_2: 3_100.0, DAY_3: 3_150.0})
    _index_hk(world, {DAY_1: 20_000.0, DAY_2: 20_400.0, DAY_3: 20_600.0})

    report = _report(world)

    assert report.benchmark.value is None
    assert report.excess is None
    assert report.window_return is not None
    assert "只出绝对收益" in report.benchmark.note
    assert "A" in report.benchmark.note and "HK" in report.benchmark.note


def test_a_day_with_non_positive_nav_is_not_used_as_the_denominator(world) -> None:
    """净值不是正数的那几天当归因起点，链乘出来的是没意义的数（D297）。

    实机那条：账户先买后入金，回填历史把曲线头部撑成负净值，屏幕上出现
    区间收益 −92,016.96%、单票区间均权 87,546.31%。负数当分母时符号与量级都失真，
    这一段就不该进百分比——宁可从第一个正净值那天算起，并如实说丢了几天。
    """
    _buy(world, "601088", 4_000, 10.0)
    _bars(world, "601088", {DAY_1: 9.5, DAY_2: 10.5, DAY_3: 10.5})
    # 存入挪到 DAY_3：DAY_1/DAY_2 于是是「拿着仓位、钱还没进来」的负净值两天
    world.db.execute(
        "UPDATE sim_cash_moves SET ts = ? WHERE reason = '存入 初始'", [_at(DAY_3)]
    )

    report = _report(world)

    assert (report.start, report.days) == (DAY_2, 2)
    assert report.dropped_days == 1
    # DAY_2→DAY_3 价格没动，存入也不算收益
    assert report.window_return == pytest.approx(0.0, abs=1e-9)
    assert _row(report, "601088").contribution == pytest.approx(0.0, abs=1e-9)


def test_another_users_account_is_not_readable_through_attribution(world) -> None:
    """别人的账户读不到：归因按 `user_id + account_id` 取账户，不是按 id 全库找。"""
    world.users.create("bob", PWD)

    with pytest.raises(StorageError):
        world.attribution.evaluate("bob", world.account.id, DAY_1, DAY_3)


def test_without_two_usable_days_the_whole_card_refuses(world) -> None:
    """净值还没重建过：整张表是空的并给原因，不给一串 0 或一排「—」。"""
    report = world.attribution.evaluate(USER, world.account.id, DAY_1, DAY_3)

    assert report.rows == ()
    assert report.window_return is None
    assert report.residual is None
    assert "nav rebuild" in report.skipped
    # 净值不是「没人跑命令才没有」：盘后 job 每晚重建（`run_nav`），只说 CLI 是假的操作指南
    assert "盘后" in report.skipped
