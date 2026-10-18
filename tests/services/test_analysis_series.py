"""`series` 读用例（U20 / spec §8.2）：指标与区间涨跌必须走同一条取数路径。

这个文件测的不是指标算术（那在 `tests/analysis/`），而是**读**：往前读多少根做预热、
读到哪为止、按谁的时钟、两条出口是不是同一个锚。指标本身算得再对，读错一段就是
一段错曲线，而它在图上一条线都不多、一条线都不少。

预热这一段尤其要钉住：`MA250` 需要的根数远超请求窗口。只按请求那段读的话，表头那些
行的均线要么是 `–`（看起来像「这只票数据不够」），要么是用六根凑出的「二十日线」——
`analysis` 层的 `null` 规则挡住了后一种，前一种就得靠「往前多读了 N 根」这句话出声。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import duckdb
import polars as pl
import pytest
from support import FrozenClock

from sams.core.errors import NotAdjustable, ValidationError
from sams.services.analysis import (
    AnalysisService,
    SeriesRead,
    default_indicators,
    indicator_names,
)
from sams.storage.db import Database
from sams.storage.migrate import apply_migrations
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.corp_actions import CorpActionRepo
from sams.storage.repos.corp_actions_coverage import CorpActionsCoverageRepo
from sams.storage.repos.symbols import SymbolRepo


def _fresh_db() -> Database:
    """Create an independent in-memory database for tests that need two separate DBs."""
    database = Database(duckdb.connect(":memory:"))
    apply_migrations(database)
    return database

CODE = "601088"
#: 六十四根日线（2024-04-01 起，按日历日排，交易日历不在这条路上）：够 MA60 出数，
#: 也够让「往前多读了 N 根」这句话当场数得出来
DAYS = [date(2024, 4, 1) + timedelta(days=i) for i in range(64)]
TODAY = DAYS[-1]  # 2024-06-03
#: 默认那次除权：落在请求段（最后 6 天）之前，两条出口都要乘上它
EX_DATE = date(2024, 5, 20)


class SpyActions(CorpActionRepo):
    """记账用的仓库替身：锁住 `series` 有没有把 PIT 上界传下去。

    少传 `until` 不会让任何一个数失败——`actions_in_span` 还会把窗口外的日期筛掉——
    它只在「未来的除权已经进了库」那种票上多乘一次。同一条口径在区间那条路上
    已经被钉过一次（`test_analysis.py`），换一条出口不能就没证据了。
    """

    def __init__(self, db) -> None:
        super().__init__(db)
        self.untils: list[date | None] = []

    def list_for(self, market: str, code: str, *, until: date | None = None) -> pl.DataFrame:
        self.untils.append(until)
        return super().list_for(market, code, until=until)


def ohlcv(days: list[date], *, base: float = 10.0, step: float = 0.05) -> pl.DataFrame:
    """小步上涨的一串日线。

    每根只涨 0.05 元（约 0.5%）：A 股那道 D37 证据闸会把超过涨跌幅的裸价跳变当成
    漏记的除权，而这里的用例大多没有除权记录——用大跳变等于让守卫替我们把
    「读得对不对」这件事糊过去。
    """
    closes = [base + step * i for i in range(len(days))]
    return pl.DataFrame(
        {
            "market": ["A"] * len(days),
            "code": [CODE] * len(days),
            "date": days,
            "open": closes,
            "high": [c + 0.10 for c in closes],
            "low": [c - 0.10 for c in closes],
            "close": closes,
            "volume": [1000.0] * len(days),
            "amount": [c * 1000.0 for c in closes],
            "source": ["akshare"] * len(days),
        }
    )


def dividend_row(ex_date: date = EX_DATE, cash: float = 0.50) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "market": ["A"],
            "code": [CODE],
            "ex_date": [ex_date],
            "kind": ["dividend"],
            "cash_per_share": [cash],
            "ratio": [None],
            "price": [None],
            "src": ["akshare"],
            "note": [f"10派{cash * 10:.2f}元(含税)"],
        }
    )


def make(
    db,
    *,
    bars: pl.DataFrame | None = None,
    actions: pl.DataFrame | None = None,
    covered_from: date | None = DAYS[0],
    covered_to: date = TODAY,
    full_history: bool = True,
    today: date = TODAY,
) -> AnalysisService:
    """一份只读得动的库：K 线、（可选）一条除权、（可选）一段覆盖确认。"""
    BarRepo(db).upsert(bars if bars is not None else ohlcv(DAYS))
    actions_repo = CorpActionRepo(db)
    if actions is not None and actions.height:
        actions_repo.upsert_many(actions)
    coverage = CorpActionsCoverageRepo(db)
    if covered_from is not None:
        coverage.record(
            "A",
            CODE,
            covered_from=covered_from,
            covered_to=covered_to,
            full_history=full_history,
            src="akshare",
        )
    return AnalysisService(
        BarRepo(db),
        actions_repo,
        coverage,
        SymbolRepo(db),
        clock=FrozenClock(datetime(today.year, today.month, today.day, 15, tzinfo=UTC)),
    )


def read(db, *, service: AnalysisService | None = None, **kwargs) -> SeriesRead:
    args: dict = {"market": "A", "code": CODE, "start": DAYS[-6], "end": TODAY}
    args.update(kwargs)
    return (service if service is not None else make(db)).series(**args)


# --- 往前读几根：这条出口与区间那条唯一的实质差别 ---------------------------------------


def test_the_series_reads_back_far_beyond_the_requested_window(db):
    """请求最后 6 根，读的是全部 64 根：MA20 在表头第一行就该有值。

    只按请求那段读的话，第一行的 `ma20` 是 `–`，屏幕上看着像「这只票的数据不够」，
    而库里明明躺着前 58 根。
    """
    result = read(db)
    assert result.warmup_bars == 58
    assert result.rows == 6
    assert result.table["ma20"].to_list()[0] is not None


def test_the_warm_up_reaches_the_coverage_and_no_further(db):
    """确认只管到 5-20 起 ⇒ 预热也只能从那天起（D36）。

    多读的那几天不是「更精确」：那段没人查过除权，乘出来的是可能漏因子的曲线，
    而它已经进了表头那个均线的分母里。
    """
    result = read(db, service=make(db, covered_from=date(2024, 5, 20), full_history=False))
    assert result.warmup_floor == date(2024, 5, 20)
    assert result.anchored_on == date(2024, 5, 20)
    # 5-20 到止日只有 15 根：MA20 凑不满，`–` 是被截断的结果，不是「这只票没数据」
    assert result.warmup_bars == 9
    assert result.table["ma20"].to_list()[0] is None


def test_a_full_history_confirmation_does_not_report_a_truncation(db):
    """全历史确认只管上界（D49）：预热一路铺到库里最早那根，没有截断可报。"""
    result = read(db, service=make(db, covered_from=date(2024, 5, 20)))
    assert result.warmup_floor is None
    assert result.anchored_on == DAYS[0]


def test_the_anchor_is_reported_because_the_absolute_level_moves_with_it(db):
    """后复权的**倍数**锚在读取的第一根：锚不同，均线那种绝对价位就整体平移。

    这条不是实现细节而是读数口径：屏幕上「MA5 = 11.32」这句话换一个锚就不是那个数了。
    库里那次除权（5-06）落在被截断那段之外，于是两段的 `close_hfq` 差一个常数因子——
    而首尾之比、以及一切比值型指标（RSI/KDJ/WR）都不受影响。
    """
    ex_date = date(2024, 5, 6)
    whole = read(db, service=make(db, actions=dividend_row(ex_date)))
    # `short` 用一个独立的库：`record()` 会把相接的区间合并，若在同一个库里再写
    # 一段窄区间，先前的全历史覆盖会把新窗口吞掉（两者相接 ⇒ 取并集、保留 full_history=True），
    # 导致预热仍然覆盖到 DAYS[0]，这条要证的事就被那次合并糊过去了。
    short_db = _fresh_db()
    short = read(
        short_db,
        service=make(
            short_db,
            actions=dividend_row(ex_date),
            covered_from=date(2024, 5, 20),
            full_history=False,
        ),
    )
    assert whole.anchored_on == DAYS[0]
    assert short.anchored_on == date(2024, 5, 20)
    # `prev_close` 是除权前一天（DAYS[34]）的裸收盘价，不是除权当天（DAYS[35]）
    prev = 10.0 + 0.05 * ((ex_date - DAYS[0]).days - 1)
    factor = prev / (prev - 0.50)
    assert short.table["ma5"].to_list()[-1] == pytest.approx(
        whole.table["ma5"].to_list()[-1] / factor, rel=1e-9
    )
    # 比值型指标不跟着锚走：这正是「绝对价要报锚、比率不用报」的那条分界
    assert short.table["rsi14"].to_list()[-1] == pytest.approx(
        whole.table["rsi14"].to_list()[-1], rel=1e-9
    )


# --- 同一条闸、同一个 PIT、同一个键形态 --------------------------------------------------


def test_the_series_path_passes_the_same_pit_bound(db):
    BarRepo(db).upsert(ohlcv(DAYS))
    spy = SpyActions(db)
    coverage = CorpActionsCoverageRepo(db)
    coverage.record(
        "A", CODE, covered_from=DAYS[0], covered_to=TODAY, full_history=True, src="akshare"
    )
    service = AnalysisService(
        BarRepo(db),
        spy,
        coverage,
        SymbolRepo(db),
        clock=FrozenClock(datetime(TODAY.year, TODAY.month, TODAY.day, 15, tzinfo=UTC)),
    )
    service.series("A", CODE, start=DAYS[-6], end=TODAY)
    assert spy.untils == [TODAY]


def test_an_unconfirmed_window_refuses_the_series_too(db):
    """指标不是「涨跌」，那道覆盖闸就不生效了？这条用例就是问这个的。"""
    with pytest.raises(NotAdjustable, match="覆盖确认"):
        read(db, service=make(db, covered_from=None))


def test_the_series_and_the_interval_agree_on_the_same_window(db):
    """两条出口、一个数：区间涨跌必须等于同一段序列的首尾之比。

    分成两条取数路径时，最先长出来的不是差别，而是「谁也没想到要对比」。
    """
    service = make(db, actions=dividend_row())
    start, end = DAYS[-30], TODAY
    span = service.interval_return("A", CODE, start, end)
    series = service.series("A", CODE, start=start, end=end, indicators=["ma"])
    hfq = series.table["close_hfq"].to_list()
    assert hfq[-1] / hfq[0] - 1 == pytest.approx(span.adjusted, rel=1e-9)
    # 裸价那一列同时在场：两条出口对「这段时间除过权」要说同一个话
    raw = series.table["close"].to_list()
    assert raw[-1] / raw[0] - 1 == pytest.approx(span.raw, rel=1e-9)


def test_a_lowercase_market_reads_the_same_series(db):
    """`--market a` 在 `series` 这条路上同样不能把 D37 静默关掉（同 `interval_return`）。"""
    service = make(db)
    lower = service.series("a", f"sh{CODE}", start=DAYS[-6], end=TODAY)
    assert (lower.market, lower.code) == ("A", CODE)
    assert lower.table.equals(service.series("A", CODE, start=DAYS[-6], end=TODAY).table)


def test_a_reversed_window_is_refused_before_the_read(db):
    with pytest.raises(ValidationError, match="空区间"):
        read(db, start=TODAY, end=DAYS[-6])


# --- 周期、指标名与缺数 ------------------------------------------------------------------


def test_an_unknown_period_is_refused_rather_than_read_as_daily(db):
    """`--period weekly` 静默按日线算，给的就不是那个人请求的那段行情。"""
    with pytest.raises(ValidationError, match="周期只认"):
        read(db, start=DAYS[-20], period="weekly")


def test_the_week_series_is_named_by_friday_and_flags_the_unfinished_one(db):
    """周线的 `date` 是日历上的周五；走完的那一周不带 `pending`。

    这些天按日历日连续排（含周末），所以每一周都有周一到周五五根，没有缺周五的那种周。
    """
    days = [date(2024, 6, 3) + timedelta(days=i) for i in range(28)]  # 到 6-30（周日）
    service = make(
        db,
        bars=ohlcv(days),
        covered_from=days[0],
        covered_to=days[-1],
        today=days[-1],
    )
    result = service.series("A", CODE, start=days[0], end=days[-1], period="week")
    dates = result.table["date"].to_list()
    assert all(d.weekday() == 4 for d in dates), "每行都该叫它那个周期的周五"
    assert result.pending_periods == []


def test_a_week_still_running_is_reported_as_pending(db):
    """时钟走到周三 ⇒ 本周那根只走了三天，它不是收盘周 K（口径写进 `pending`）。"""
    days = [date(2024, 6, 3) + timedelta(days=i) for i in range(23)]  # 到 6-25（周二）
    service = make(
        db,
        bars=ohlcv(days),
        covered_from=days[0],
        covered_to=days[-1],
        today=days[-1],
    )
    result = service.series("A", CODE, start=days[0], end=days[-1], period="week")
    assert result.pending_periods == [date(2024, 6, 28)]


def test_an_unknown_indicator_name_is_refused_with_the_list(db):
    """写错一个名字就说「没有这个指标」并列出能用的，而不是少给一栏。"""
    with pytest.raises(ValidationError, match="不认识的指标名") as exc:
        read(db, indicators=["madd"])
    assert "ma" in str(exc.value)


def test_duplicated_names_are_collapsed(db):
    """`--indicators ma,ma` 是同一个意思敲了两遍，不是两个指标。"""
    assert read(db, indicators=["ma", "ma"]).indicators == ("ma",)


def test_the_default_names_are_the_ones_the_cli_lists(db):
    """注册表只有一份：命令行 `--indicators` 的取值表与服务层默认值来自同一个地方。"""
    assert set(default_indicators()) <= set(indicator_names())
    assert read(db).indicators == default_indicators()


def test_a_missing_high_price_stops_kdj_but_not_ma(db):
    """缺最高价只挡住用得到它的那几组：`--indicators ma` 照旧出数。

    「整条序列里有一根缺数 ⇒ 什么都不给」会把本来能用的那部分也拒了；
    「跳过那根继续递推」会让之后的整条曲线与正解不同。两条都不选，所以按列分。
    """
    holed = ohlcv(DAYS).with_columns(
        pl.when(pl.col("date") == DAYS[10])
        .then(None)
        .otherwise(pl.col("high"))
        .alias("high")
    )
    service = make(db, bars=holed)
    args = {"market": "A", "code": CODE, "start": DAYS[-6], "end": TODAY}
    with pytest.raises(ValidationError, match="high_hfq"):
        service.series(**args, indicators=["kdj"])
    assert service.series(**args, indicators=["ma"]).rows == 6


def test_a_window_with_no_bars_says_where_the_bars_are(db):
    service = make(db)
    with pytest.raises(NotAdjustable, match="最新到") as exc:
        service.series("A", CODE, start=date(2025, 1, 1), end=date(2025, 1, 31))
    assert str(TODAY) in str(exc.value)
