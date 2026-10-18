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


def ohlcv(days: list[date], *, base: float = 10.0, step: float = 0.05,
          closes: list[float] | None = None) -> pl.DataFrame:
    """小步上涨的一串日线（或调用方自带的收盘价列）。

    每根只涨 0.05 元（约 0.5%）：A 股那道 D37 证据闸会把超过涨跌幅的裸价跳变当成
    漏记的除权，而这里的用例大多没有除权记录——用大跳变等于让守卫替我们把
    「读得对不对」这件事糊过去。
    """
    closes = closes if closes is not None else [base + step * i for i in range(len(days))]
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


def wiggle(days: list[date], *, base: float = 10.0, step: float = 0.05) -> list[float]:
    """小步上涨 + 每日交替 ±0.35 的抖动：有涨有跌，RSI 才会落在 (0, 100) 而不是天花板。

    单调序列上任何 RSI 用例都等于 100——「两个读数相等」在这种数据上是平凡真的，
    什么都证不了。抖动后单根变动约 6%，仍在 A 股涨跌幅的 1.5 倍阈值之内，
    D37 不会把它当成漏记的除权拦下来。
    """
    return [base + step * i + (0.35 if i % 2 else -0.35) for i in range(len(days))]


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


def unknown_row(ex_date: date) -> pl.DataFrame:
    """一条 D39 会拦下来的公司行为：源报了有这件事，但折不出数。

    这类记录是真实存在的：00700 在 2023-01-05 的那次实物分派就是这个形状。
    这里用 A 市场只是为了与其余用例共用同一批 K 线；D39 不分市场。
    """
    return pl.DataFrame(
        {
            "market": ["A"],
            "code": [CODE],
            "ex_date": [ex_date],
            "kind": ["unknown"],
            "cash_per_share": [None],
            "ratio": [None],
            "price": [None],
            "src": ["akshare"],
            "note": ["实物分派：每股分派附属公司 B 类股，折算单价源未给出"],
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
    # 「不是 None」只证明凑满了 20 根——预热 19 根时它也成立。这里把用的是哪 20 根
    # 也钉住：表头第一行的 MA20 必须等于读取序列里截至该行的 20 根（其中 19 根是
    # 没被请求的预热根）的均值，而不是请求段自己那几根凑出来的任何东西。
    days = result.series.frame["date"].to_list()
    hfq = result.series.frame["close_hfq"].to_list()
    first = days.index(result.requested_from)
    assert first == 58
    window = hfq[first - 19 : first + 1]
    assert len(window) == 20
    assert result.table["ma20"].to_list()[0] == pytest.approx(sum(window) / 20, rel=1e-12)


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
    库里那次除权（5-06）落在被截断那段之外，于是两段的 `close_hfq` 差一个常数因子。
    数据用带抖动的（`wiggle`）：单调序列上 RSI 恒等于 100，「比值不变」在那种数据上
    是平凡真的，证不了任何事——这里有涨有跌，RSI 落在 (0, 100) 中间。
    """
    ex_date = date(2024, 5, 6)
    closes = wiggle(DAYS)
    bars = ohlcv(DAYS, closes=closes)
    whole = read(db, service=make(db, bars=bars, actions=dividend_row(ex_date)))
    # `short` 用一个独立的库：`record()` 会把相接的区间合并，若在同一个库里再写
    # 一段窄区间，先前的全历史覆盖会把新窗口吞掉（两者相接 ⇒ 取并集、保留 full_history=True），
    # 导致预热仍然覆盖到 DAYS[0]，这条要证的事就被那次合并糊过去了。
    short_db = _fresh_db()
    short = read(
        short_db,
        service=make(
            short_db,
            bars=bars,
            actions=dividend_row(ex_date),
            covered_from=date(2024, 5, 20),
            full_history=False,
        ),
    )
    assert whole.anchored_on == DAYS[0]
    assert short.anchored_on == date(2024, 5, 20)
    # `prev_close` 是除权前一天（DAYS[34]）的裸收盘价，不是除权当天（DAYS[35]）
    prev = closes[(ex_date - DAYS[0]).days - 1]
    factor = prev / (prev - 0.50)
    assert short.table["ma5"].to_list()[-1] == pytest.approx(
        whole.table["ma5"].to_list()[-1] / factor, rel=1e-9
    )
    # 比值型指标不跟着锚走，正确的形式是「整段同乘一个常数时不变」：short 那 15 根
    # 全在除权日之后，close_hfq 与裸价只差锚给出的同一个常数，它的 rsi14 第一个值
    # 恰好落在表尾那行（14 个涨跌正好凑满窗口），手算 Wilder 种子必须与之一致。
    # 注意不能拿 whole 的 rsi14 来等：那条的平滑带着 4 月起的整段历史，与 short 的
    # 截断种子本来就该不同——「相等」若成立只因数据单调、两个都钉在 100 的天花板上。
    hfq = short.series.frame["close_hfq"].to_list()
    assert len(hfq) == 15
    gains = [max(c - p, 0.0) for p, c in zip(hfq[:-1], hfq[1:], strict=True)]
    losses = [max(p - c, 0.0) for p, c in zip(hfq[:-1], hfq[1:], strict=True)]
    avg_gain, avg_loss = sum(gains) / 14, sum(losses) / 14
    expected = 100 * avg_gain / (avg_gain + avg_loss)
    assert 0.0 < expected < 100.0, "数据若做不到有涨有跌，这条就退化成天花板上的平凡相等"
    assert short.table["rsi14"].to_list()[-1] == pytest.approx(expected, rel=1e-9)


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


def test_an_unmarked_jump_in_the_warm_up_stops_the_series_too(db):
    """D37 也管预热段：跳变落在请求窗口之前，一样不能只因为「表里看不见」就放行。

    `series` 比 `interval_return` 多读了一大段没被请求的过去；若闸只拦请求窗口内的
    跳变，这条新出口就成了绕过 D37 的那条路——均线分母里乘进一条漏了因子的曲线，
    而屏幕上没有任何一处会说「这段的复权证据不齐」。区间那条路的同款用例在
    `tests/analysis/test_adjust.py`；换一条出口不能就没证据了。
    """
    closes = [10.0 + 0.05 * i for i in range(len(DAYS))]
    jump = 30  # 2024-05-01：落在请求段（最后 6 根）之前，表里看不见它，递推却吃得着
    for i in range(jump, len(closes)):
        closes[i] *= 0.7
    service = make(db, bars=ohlcv(DAYS, closes=closes))
    with pytest.raises(NotAdjustable, match="裸价变动") as exc:
        service.series("A", CODE, start=DAYS[-6], end=TODAY)
    assert str(DAYS[jump]) in str(exc.value)


def test_a_warm_up_blocker_does_not_lock_the_series_out_forever(db):
    """D39 自救指引（「起日 ≥ 该除权日」）在带预热的 `series` 出口上曾经失效。

    `interval_return` 只取请求段首尾，把起日挪过除权日就能躲开那条 unknown；
    `series` 多读了一大段预热，`_warmup_span` 按全历史确认一路回读到库底，预热段
    照样横跨那条记录——交易员照文案挪了起日，还是被拒，那只票从挪不动的那天起
    永远拿不到复权序列（00700 2023-01-05 那次实物分派就是这种形状）。
    修法：把 `read_from` 推到 unknown 记录那天为止（`actions_in_span` 用 `> first`，
    等号那天不再进因子），同时把截断处如实报进 `warmup_floor`。
    """
    blocker = EX_DATE  # DAYS[49] = 2024-05-20；请求段从 DAYS[-6]=2024-05-29 起，比它晚
    result = read(db, service=make(db, actions=unknown_row(blocker)))
    assert result.rows > 0, "请求段在 blocker 之后，不应被永久锁死"
    # 锚与预热截断都落在那条 unknown 记录那天：从那根起 adj_factor = 1
    assert result.anchored_on == blocker
    assert result.warmup_floor == blocker
    # 预热只到 blocker，再往前的 49 根没有进递推
    assert result.warmup_bars == sum(1 for d in DAYS if blocker <= d < DAYS[-6])


def test_a_blocker_at_the_start_edge_also_opens_the_series(db):
    """D39 文案说「起日 ≥ 该除权日」——等号那侧也要算，否则那句指引还是半句真话。

    `interval_return` 里等号就够（`actions_in_span` 用 `> first`，first=start 时
    除权日当天不进因子）；`series` 因为多读预热，等号也得把 `read_from` 推到那天，
    否则预热照样跨过去，指引与实现不符。
    """
    blocker = EX_DATE
    result = read(db, service=make(db, actions=unknown_row(blocker)), start=blocker)
    assert result.rows > 0
    assert result.anchored_on == blocker
    assert result.warmup_bars == 0  # read_from == start，没有往回多读


def test_a_blocker_inside_the_requested_window_still_refuses(db):
    """修法只管预热段；请求段自己盖着 unknown，守卫照旧拒——这不是回归而是设计。

    挪起日能躲开的只有「除权日在请求段之前」那一侧；把起日放到 unknown 之前，
    那条行为就在用户真正要的那段区间里，没有任何诚实的复权数可给。
    """
    blocker = EX_DATE
    with pytest.raises(NotAdjustable, match="无法折算"):
        read(db, service=make(db, actions=unknown_row(blocker)), start=DAYS[40])


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
    # 逐个点名这四个周五：`all(d.weekday() == 4)` 在空表上会平凡通过，
    # 而「表是空的」恰恰是这条路上最像成功的不成功
    assert result.table["date"].to_list() == [
        date(2024, 6, 7),
        date(2024, 6, 14),
        date(2024, 6, 21),
        date(2024, 6, 28),
    ]
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


def test_period_warm_up_counts_daily_bars_not_period_rows(db):
    """月线说「预热 0 根」却把整个 4 月喂进了递推——预热根数要按日线计（交易员闸第 2 项）。

    `start = 2024-05-01`：4 月的 30 根日线全部参与 5 月那根月 K 的指标递推，
    而按重采样后 `date`（月标签）数的话只有 1 行（4-30 < 5-01），
    屏幕因此说「预热 1 根」，把一个完整的 4 月藏在了读数背后。
    同理，`anchored_on` 若也按周期标签报，打出来是 2024-04-30（非交易日）而不是
    真实的 hfq=1 锚（4 月第一根日线 = DAYS[0] = 2024-04-01）。
    """
    may1 = DAYS[30]  # 2024-05-01
    assert may1 == date(2024, 5, 1)
    result = read(db, service=make(db), start=may1, end=TODAY, period="month")
    # 日线口径：30 根 4 月日线在 start 之前参与了递推
    assert result.warmup_bars == 30
    # 锚在日线窗口第一根，不是月标签（2024-04-30 是日历月末，不是交易日）
    assert result.anchored_on == DAYS[0]


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
