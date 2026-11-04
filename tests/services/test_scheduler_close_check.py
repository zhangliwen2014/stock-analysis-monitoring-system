"""`close-check`：当晚的全市场收盘快照是当天日线收盘的见证（D301）。

这一趟不联网、不改库里的价，只做三件事——比出来、把对不上的排进补数队列、留一行读数。
「请你重采日线」这类话不该出现在界面上（用户定的原则：数据不全由后台自动处理），
所以这里断言的是**队列里多了哪只票**与**屏幕上那句话**，不是有没有弹窗提醒人。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import polars as pl
import support_valuation as sample
from support import FrozenClock

from sams.datasource.registry import AdapterRegistry
from sams.services.market_snapshot import MarketSnapshotService
from sams.services.scheduler.due import DAILY
from sams.services.scheduler.jobs import build_jobs, run_close_check
from sams.services.scheduler.types import JobContext, JobDeps
from sams.services.scheduler.universe import Universe
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.snapshots import SnapshotRepo
from sams.storage.repos.symbol_backfills import BARS, SymbolBackfillRepo
from sams.storage.repos.symbol_list import SymbolListRepo

AS_OF = date(2024, 10, 25)
TAKEN_AT = datetime(2024, 10, 25, 12, 0, tzinfo=UTC)


class SpotStub:
    """只为了让探活认得出「这个市场有快照源」。注册表那两枚用例不会调用它。"""

    name = "tencent"
    markets = frozenset({"A"})

    def market_snapshot(self, market, codes):
        raise AssertionError("注册表用例不该取数")


def write_bars(db, closes: dict[str, float]) -> None:
    """按只写当天一根日线，收盘就是给的那个数。"""
    for code, close in closes.items():
        BarRepo(db).upsert(
            sample.bars([AS_OF], [close], market="A", code=code)
        )


def write_snapshot(db, prices: dict[str, float]) -> None:
    """当晚那一篮全市场快照：只有 `code` 与 `price` 参与对账，其余列给个值让仓库收得下。"""
    codes = list(prices)
    SnapshotRepo(db).replace_day(
        market="A",
        day=AS_OF,
        taken_at=TAKEN_AT,
        frame=pl.DataFrame(
            {
                "code": codes,
                "name": [f"票{c}" for c in codes],
                "price": [prices[c] for c in codes],
                "pct_chg": [0.0] * len(codes),
                "amount": [1e8] * len(codes),
                "turnover_rate": [1.0] * len(codes),
                "volume_ratio": [1.0] * len(codes),
                "amplitude": [1.0] * len(codes),
                "pe_ttm": [10.0] * len(codes),
                "pe_dyn": [9.0] * len(codes),
                "pb": [1.2] * len(codes),
                "market_cap": [100.0] * len(codes),
                "float_cap": [90.0] * len(codes),
                "is_st": [False] * len(codes),
                "source": ["tencent"] * len(codes),
            }
        ),
    )


def deps(db, settings, *, snapshots=None, spot=False, screener=False, queue=True):
    """`spot=True` 给注册表挂一个有快照能力的源——登记那两枚用例问的就是这个。

    那个源在这里永远不会被调用：`build_jobs` 只看探活，取数是 job 跑起来以后的事。
    `queue=False` 抽掉补数队列：对账没有可登记的地方，那一趟就不该出现在注册表里。
    """
    adapters = {"tencent": SpotStub()} if spot else {}
    market_sources = {"A": ("tencent",)} if spot else {}
    registry = AdapterRegistry(
        adapters=adapters, market_sources=market_sources, sleep=lambda _s: None
    )
    return JobDeps(
        settings=settings,
        registry=registry,
        sync=SimpleNamespace(bars=BarRepo(db), indices=None),  # type: ignore[arg-type]
        corp_actions=None,  # type: ignore[arg-type]
        fundamentals=None,  # type: ignore[arg-type]
        f10=None,  # type: ignore[arg-type]
        fx=SimpleNamespace() if spot else None,  # type: ignore[arg-type]
        calendar=SimpleNamespace() if spot else None,  # type: ignore[arg-type]
        rules=SimpleNamespace() if spot else None,  # type: ignore[arg-type]
        recheck=SimpleNamespace() if spot else None,  # type: ignore[arg-type]
        users=None,  # type: ignore[arg-type]
        snapshots=snapshots
        if snapshots is not None
        else MarketSnapshotService(
            registry,
            SymbolListRepo(db),
            SnapshotRepo(db),
            clock=FrozenClock(TAKEN_AT),
        ),
        screener_monitor=SimpleNamespace() if screener else None,
        backfills=SymbolBackfillRepo(db) if queue else None,
    )


def ctx() -> JobContext:
    return JobContext(
        period_key="2024-10-25",
        as_of=AS_OF,
        market="A",
        universe=Universe(by_market={"A": ()}, fx_pairs=()),
    )


def open_rows(db) -> list[tuple[str, str]]:
    return [(row.code, row.kind) for row in SymbolBackfillRepo(db).take_pending(limit=100)]


def test_a_close_that_disagrees_with_the_snapshot_is_enqueued_for_a_repull(db, settings):
    """对不上就不问人：登记一次**日线**重采，读数里点名那几只。

    600519 库里 10.00、快照 10.05（0.5%）；601088 两边都是 10.00。
    队列里的活分两种（`bars`/`actions`），排错那种等于没排——所以连 kind 一起断言。
    """
    write_bars(db, {"600519": 10.00, "601088": 10.00})
    write_snapshot(db, {"600519": 10.05, "601088": 10.00})

    outcome = run_close_check(deps(db, settings), ctx())

    assert outcome.rows_affected == 1
    assert open_rows(db) == [("600519", BARS)]
    assert "600519" in outcome.note


def test_matching_closes_say_so_and_enqueue_nothing(db, settings):
    """相符的那一晚也要有一句「比对过、相符」，队列里一格都不能多。"""
    write_bars(db, {"600519": 10.00, "601088": 20.00})
    write_snapshot(db, {"600519": 10.00, "601088": 20.00})

    outcome = run_close_check(deps(db, settings), ctx())

    assert outcome.rows_affected == 0
    assert open_rows(db) == []
    assert "0 只不平" in outcome.note


def test_the_tolerance_is_a_relative_difference_not_a_number_of_fen(db, settings):
    """判据是相对差：低价票的一档与高价票的几分钱不该同权。

    5.38 → 5.48 差 0.10 元（1.86%，实机 2026-10-29 的 600018 就是这个形状）；
    1000.00 → 1000.05 只差 0.005%，算相符。
    """
    write_bars(db, {"600018": 5.38, "601999": 1000.00})
    write_snapshot(db, {"600018": 5.48, "601999": 1000.05})

    outcome = run_close_check(deps(db, settings), ctx())

    assert outcome.rows_affected == 1
    assert open_rows(db) == [("600018", BARS)]


def test_a_night_without_a_snapshot_reports_that_instead_of_zero(db, settings):
    """没有见证就说没有见证，不报「0 只不平」：那句话会把「没比对」读成「比对过且相符」。"""
    write_bars(db, {"600519": 10.00})

    outcome = run_close_check(deps(db, settings), ctx())

    assert outcome.rows_affected == 0
    assert "没有收盘快照" in outcome.note


def test_a_code_missing_from_the_bars_is_not_called_a_mismatch(db, settings):
    """快照里有、库里当天没 K 线：最常见的解释是停牌，不算不平。

    把它算进去，一晚会有几十到几百只票进队列，而它们补不出任何行——真正的缺口反而
    排不上那份预算。
    """
    write_bars(db, {"600519": 10.00})
    write_snapshot(db, {"600519": 10.00, "600018": 5.48})

    outcome = run_close_check(deps(db, settings), ctx())

    assert outcome.rows_affected == 0
    assert open_rows(db) == []


def test_build_jobs_registers_close_check_right_after_the_witness(db, settings):
    """排在 `spot` 之后、`screener` 之前：它比的是今晚这一张快照，早一步读到的是昨天那张。

    这一枚问的是注册表那一格，不是跑出来的读数——登记漏一行不会报错，只会让对账从不发生。
    """
    jobs = build_jobs(deps(db, settings, spot=True, screener=True))
    titles = [job.title for job in jobs]
    assert titles.index("spot@A") < titles.index("close-check@A")
    assert titles.index("close-check@A") < titles.index("screener@A")
    assert {job.title: job for job in jobs}["close-check@A"].period == DAILY


def test_build_jobs_registers_nothing_to_audit_without_a_witness_or_a_queue(db, settings):
    """没有快照源的市场不登记（HK/US 现在就是这样）；没有补数队列也不登记。

    两处都是「登记了也只能念一句没比对」的东西——注册表里少一行，比多一行诚实。
    """
    no_spot = [job.title for job in build_jobs(deps(db, settings, screener=True))]
    assert "close-check@A" not in no_spot

    no_queue = [job.title for job in build_jobs(deps(db, settings, spot=True, queue=False))]
    assert "close-check@A" not in no_queue


def write_bars_on(db, code: str, days: list[date], close: float = 10.0) -> None:
    """按只写几天的日线：缺整根那条判据问的是「这只票最近还有没有线」。"""
    BarRepo(db).upsert(sample.bars(days, [close] * len(days), market="A", code=code))


def test_a_whole_missing_bar_with_a_recent_witness_is_enqueued(db, settings):
    """快照有价、当天没那根 K 线、而这只票几天前还有线 ⇒ 断链，排一次重采（#380 F2）。

    内连接那一条看不见这种缺口（11-04 那晚念的是「0 只不平」），于是断了一根与
    本来就不交易在对账里是同一个答案——而前者能补、也该补。
    """
    write_bars(db, {"600519": 10.00})  # 这个市场当天已经落了线
    write_bars_on(db, "601088", [AS_OF - timedelta(days=3)])
    write_snapshot(db, {"600519": 10.00, "601088": 20.00})

    outcome = run_close_check(deps(db, settings), ctx())

    assert open_rows(db) == [("601088", BARS)]
    assert outcome.rows_affected == 1
    assert "1 只缺整根" in outcome.note


def test_a_bar_missing_for_a_month_is_still_suspension(db, settings):
    """断了一个月的那种照旧不算缺口：停牌票的日线是成片没有，断链票是最近连着有。"""
    write_bars(db, {"600519": 10.00})
    write_bars_on(db, "601088", [AS_OF - timedelta(days=30)])
    write_snapshot(db, {"600519": 10.00, "601088": 20.00})

    outcome = run_close_check(deps(db, settings), ctx())

    assert open_rows(db) == []
    assert outcome.rows_affected == 0


def test_a_market_that_landed_nowhere_is_left_to_the_bars_job(db, settings):
    """整个市场当天都没落：那是日线 job 那一层的活，不往单票队列里塞几千行。

    11-02 那天没有任何市场跑过日线，这条扫描那天会一口气报出几千只、每只都是 730 天的
    补采，把真正等补的那几只挤在后面——所以它只在"当天已有别的票落了线"时才点名。
    """
    write_bars_on(db, "601088", [AS_OF - timedelta(days=3)])
    write_snapshot(db, {"601088": 20.00})

    outcome = run_close_check(deps(db, settings), ctx())

    assert open_rows(db) == []
    assert "交给日线" in outcome.note
