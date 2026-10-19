"""估值合成的读数用例（M2-D，spec §8.3）：四路数据拼成一个分、一条结论。

这一层不算任何东西（算式在 `analysis/valuation/`），它只决定**喂进去的是哪几个数**，
而五个地方会静默出错：

1. **现价来自哪一路**。`valuation_daily` 没有价格列（D60），那个数只能来自过闸的日线；
   而同一条序列里既有裸价又有后复权价，两者差着当日因子。合成里的四个价位全都与**现价**
   比大小，所以喂给它们的必须是同一个价空间（见除权那一组用例）。
2. **模板没问的不许算**（D70）。`composite` 会拒收权重表里没有的分项；多算一路不只是慢，
   它会让屏幕上多出一栏没人负责的解释。
3. **PIT 的逐日版本**。分位曲线里每一天有自己的截至日，所以它要吃**含无披露日**的那些期：
   仓库侧先滤掉的话 `unpublished_periods` 恒为 0，那句「另有 N 期未被采信」就没出处了。
4. **样本不够就说不够**。窗口内只有 60 天时给「10 分位」是把抽样噪声说成估值位置；拒掉
   那一项之后合成分仍然可以出，但 `missing` 里要点名它，也不许补一个「中性」的 50 分。
5. **0 行不是「没有」**。港美股在库里没有基本面行时，屏幕上要有一句下一步该跑的命令，
   而不是一个恰好很低的分。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime

import polars as pl
import pytest
import support
from support_valuation import (
    CLOSES,
    CODE,
    DAYS,
    FY22,
    FY23,
    MARKET_CAP,
    PE,
    PR_NOW,
    PRICE,
    TODAY,
    dividend,
    financial_rows,
    flow_rows,
    seed,
)

from sams.analysis.valuation.percentile import MIN_SAMPLES
from sams.analysis.valuation.styles import TREND, VALUE
from sams.core.errors import NotAdjustable
from sams.services.analysis import AnalysisService
from sams.services.fundamentals import FundamentalService
from sams.services.valuation import ValuationService
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.corp_actions import CorpActionRepo
from sams.storage.repos.corp_actions_coverage import CorpActionsCoverageRepo
from sams.storage.repos.financials import FinancialRepo, FundingFlowRepo, ValuationRepo
from sams.storage.repos.symbols import SymbolRepo

#: 样本数据（300 天 K 线、两期年报、逐日估值）在 `tests/support_valuation.py`：
#: 命令行那一侧要对着同一批数验屏幕上的话，两份样本分叉就是各自对自己的数点头


def build(
    db,
    *,
    market: str = "A",
    code: str = CODE,
    days: list[date] | None = None,
    closes: list[float] | None = None,
    actions: pl.DataFrame | None = None,
    flow: pl.DataFrame | None = None,
    with_bars: bool = True,
    with_fundamentals: bool = True,
    today: date = TODAY,
) -> ValuationService:
    """一份读得动的库（`support_valuation.seed`）+ 只连着它的三个读数用例。"""
    seed(
        db,
        market=market,
        code=code,
        days=days,
        closes=closes,
        actions=actions,
        flow=flow,
        with_bars=with_bars,
        with_fundamentals=with_fundamentals,
    )
    clock = support.FrozenClock(datetime(today.year, today.month, today.day, 15, tzinfo=UTC))
    analysis = AnalysisService(
        BarRepo(db),
        CorpActionRepo(db),
        CorpActionsCoverageRepo(db),
        SymbolRepo(db),
        clock=clock,
    )
    fundamentals = FundamentalService(FinancialRepo(db), ValuationRepo(db), clock=clock)
    return ValuationService(
        fundamentals,
        analysis,
        FinancialRepo(db),
        ValuationRepo(db),
        FundingFlowRepo(db),
        clock=clock,
    )



def lean_value(drop: str):
    """把某个分项从价值型的权重表里删掉（`with_overrides` 只能改权重，不能取消一项）。"""
    return replace(VALUE, weights={k: w for k, w in VALUE.weights.items() if k != drop})


def item(read, key: str):
    found = [row for row in read.composite.items if row.key == key]
    assert found, f"{key} 不在合成里：{[row.key for row in read.composite.items]}"
    return found[0]


def reading(read, metric: str):
    found = [row for row in read.percentiles if row.metric == metric]
    assert found, f"没算 {metric} 的分位：{[row.metric for row in read.percentiles]}"
    return found[0]


# --- 现价那一路：四个价位全都与它比大小 -------------------------------------------------


def test_the_price_is_the_last_gated_daily_close(db):
    """`valuation_daily` 里没有价格列（D60）：合成左边那个「现价」只能来自过闸的日线。

    黄金手算链：现价 17.0、PR = 21.03 ÷ 0.20 ÷ 100 = 1.0515 ⇒ 合理下限
    17.0 × 0.8 ÷ 1.0515 = 12.94，止损 17.0 × 0.92 = 15.64。
    """
    read = build(db).read("A", CODE)
    assert PRICE == 17.0, "整条黄金链都按末根收盘 17.0 手算，改样本就得重算"
    assert read.price == pytest.approx(PRICE)
    assert (read.price_on, read.price_age_days) == (TODAY, 0)
    assert read.fundamental.pr.value == pytest.approx(PR_NOW)
    assert read.plan.fair.low == pytest.approx(PRICE * 0.8 / PR_NOW)
    assert read.plan.stop.low == pytest.approx(PRICE * 0.92)


def test_the_target_prefers_the_nearer_leg_and_shows_both(db):
    """合理上限 19.40 比前高 19.97 更近 ⇒ 取前者，但前高那个数也要能在屏幕上被看见。"""
    read = build(db).read("A", CODE)
    assert read.recent_high == pytest.approx(19.97)
    assert read.plan.target.low == pytest.approx(read.plan.fair.high)
    assert "前高" in read.plan.target.evidence
    assert read.plan.ok
    assert read.plan.odds.value == pytest.approx(
        (read.plan.target.low - PRICE) / (PRICE - read.plan.stop.low)
    )


def test_a_symbol_with_no_bars_refuses_the_whole_read(db):
    """没有日线就没有现价，而现价是那条式子的左边：不给一个「只有基本面」的半条结论。

    半条结论在屏幕上与一条完整的长得像（合成分照出，只是价位那几行是空的），
    而它的意思其实是「这只票我们连价都不知道」。
    """
    with pytest.raises(NotAdjustable):
        build(db, with_bars=False).read("A", CODE)


# --- 价空间：后复权的指标值必须折回现价口径 ---------------------------------------------


def test_a_dividend_inside_the_window_does_not_lift_the_stop_above_the_price(db):
    """除权日之后价格「抬高一档」是复权序列的本分，不是市场的出价。

    常数收盘 10.0 + 早期一次每股派 1.0 元 ⇒ 除权后 hfq = 10 ÷ 0.9 = 11.1111，而现价还是
    10.0。不折回来的话周MA20 止损会读成 11.11（在现价**上方**），屏幕上那句
    「收盘跌破即走」就在说一个市场没出过的价。
    """
    flat = [10.0] * len(DAYS)
    read = build(db, closes=flat, actions=dividend(DAYS[10])).read("A", CODE, style="trend")
    assert read.price == pytest.approx(10.0)
    assert read.weekly_ma20 == pytest.approx(10.0), "折回现价口径之后它就该是 10.0"
    assert read.recent_high == pytest.approx(10.0)
    assert read.plan.stop.low == pytest.approx(10.0)


def test_the_fold_back_is_only_spoken_when_there_is_something_to_fold(db):
    """没有除权的序列里 hfq == 裸价，这时那句「按当日因子折回」是一句没有内容的场面话。"""
    assert not any("折回" in note for note in build(db).read("A", CODE).notes)
    got = build(db, actions=dividend(DAYS[10])).read("A", CODE)
    assert any("折回" in note for note in got.notes)


# --- 模板问什么就算什么（D70） ----------------------------------------------------------


def test_only_the_items_the_style_asks_for_are_computed(db):
    trend = build(db).read("A", CODE, style="trend")
    assert {row.key for row in trend.composite.items} == set(TREND.asked())
    assert [row.metric for row in trend.percentiles] == ["pr"], "趋势型不问 PE/PB 分位"

    value = build(db).read("A", CODE)
    assert [row.metric for row in value.percentiles] == ["pr", "pe_ttm", "pb"]
    assert item(value, "boll_position").ok
    assert item(value, "pe_percentile").ok and item(value, "pb_percentile").ok


def test_the_entry_gate_reads_pr_percentile_even_when_the_style_does_not_score_it(db):
    """买入闸门吃的是 PR 分位，与「这个模板有没有给这一项分配权重」是两件事。

    把分位从权重表里删掉就当它不存在，闸门会静默变成「不设闸」——而那正是这一档规则
    唯一的否决条件。
    """
    read = build(db).read("A", CODE, style=lean_value("pr_percentile"))
    assert "pr_percentile" not in {row.key for row in read.composite.items}
    assert reading(read, "pr").value == pytest.approx(0.0)
    assert "通过闸门" in read.plan.entry.evidence, "闸门是按分位 0 判过的"


def test_the_gate_is_answered_by_the_pr_curve_not_the_pe_one(db):
    """PE 分位低不等于 PR 分位低（D59 跨支不可比）：两个读数要各自说清分子与分母。"""
    read = build(db).read("A", CODE)
    assert reading(read, "pr").current == pytest.approx(PR_NOW)
    assert "financials.roe" in reading(read, "pr").evidence
    assert reading(read, "pe_ttm").current == pytest.approx(PE[-1])
    assert "financials.roe" not in reading(read, "pe_ttm").evidence, "PE 那条曲线没配分母"


# --- 样本地板与窗口：不够就说不够 -------------------------------------------------------


def test_a_thin_window_refuses_the_percentile_but_not_the_whole_score(db):
    """60 天样本给不出分位（那是抽样噪声），但合成分照出，只是 `missing` 里点名它。

    补一个「中性」的 50 分会把这一项从「没数据」变成「数据很平均」，权重的缺口就此消失。
    """
    read = build(db, days=DAYS[:60], closes=CLOSES[:60]).read("A", CODE)
    assert not reading(read, "pr").ok
    assert str(MIN_SAMPLES) in reading(read, "pr").reason
    assert "pr_percentile" in read.composite.missing
    assert read.composite.ok, "价值型剩下的权重够地板，不该因为少一路就整个不出数"
    assert "50" in item(read, "pr_percentile").reason


def test_the_target_label_counts_the_bars_the_library_actually_has(db):
    """库里只有 60 根时说「过去 250 日高点」是在引用一个没查过的区间。

    取下跌那一段：单调上涨的 60 根里现价就是最高，`pick_target` 那一路整个被排除，
    那句带天数的措辞根本不会出现。
    """
    read = build(db, days=DAYS[-60:], closes=CLOSES[-60:]).read("A", CODE)
    assert read.recent_high == pytest.approx(CLOSES[-60])
    assert "过去 60 日" in read.plan.target.evidence
    assert "过去 250 日" not in read.plan.target.evidence
    assert any("前高只用了 60 根" in note for note in read.notes)


def test_an_open_weekly_bar_is_called_out(db):
    """截至日落在周中 ⇒ 最后一根周 K 只走了几天，而它与一根定型的周 K 在屏幕上同形。

    那个周期在表上标的是**日历收市日**（周五），比截至日还晚：不说清「还没走完」，
    屏幕上就会出现一根日期在未来的周 K。
    """
    midweek = DAYS[289]
    assert midweek == date(2024, 10, 16), "用例挑的是周三：那一周只走了三天"
    read = build(db, days=DAYS[:290], closes=CLOSES[:290], today=midweek).read(
        "A", CODE, as_of=midweek, style="trend"
    )
    assert read.open_weekly_period == date(2024, 10, 18)
    assert any("还没走完" in note and "2024-10-18" in note for note in read.notes)


# --- PIT 的两副面孔：逐日的与截至日的 ----------------------------------------------------


def test_the_percentile_curves_see_the_undated_periods_the_pr_refuses(db):
    """同一句「另有 N 期因为没有披露日而未被采信」在两条出口上必须是同一个 N。

    分位曲线吃的是**全部**报告期（每一天自己套自己的闸门）：仓库侧先把 NULL 披露日滤掉的话
    `unpublished_periods` 恒为 0，而 `sams analysis fundamentals` 同一屏上会报 1。
    """
    rows = financial_rows().with_columns(
        pl.when(pl.col("report_date") == FY23)
        .then(pl.lit(None, dtype=pl.Date))
        .otherwise(pl.col("published_date"))
        .alias("published_date")
    )
    service = build(db, with_fundamentals=False)
    FinancialRepo(db).upsert(rows)
    read = service.read("A", CODE)
    assert read.fundamental.dropped_unpublished == 1
    assert reading(read, "pr").unpublished_periods == 1
    assert read.fundamental.period == FY22, "没有披露日的那一期不参与采信"


def test_no_row_after_the_as_of_date_votes(db):
    """把截至日挪回 2024-06-30：那之后的收盘价、估值行都不许进任何一个数。

    基本面那一侧的闸门是逐期比披露日：2024-03-20 公开的 2023 年报这天已经采信了，
    而把它再往前挪到 2024-02-29，同一只票就得回落到 2022 年报。
    """
    read = build(db).read("A", CODE, as_of=date(2024, 6, 30))
    earlier = build(db).read("A", CODE, as_of=date(2024, 2, 29))
    assert read.as_of == date(2024, 6, 30)
    assert read.price_on == date(2024, 6, 30)
    assert read.price == pytest.approx(CLOSES[DAYS.index(date(2024, 6, 30))])
    assert reading(read, "pr").window_to == date(2024, 6, 30)
    assert read.fundamental.period == FY23, "2024-03-20 公开的那期此时已经算数"
    assert earlier.fundamental.period == FY22, "那之后的报告期在此刻还不存在"


def test_the_default_as_of_comes_from_the_clock(db):
    read = build(db).read("A", CODE)
    assert read.as_of == TODAY, "缺省截至日由注入的时钟给，不等真实今天"


# --- 资金流：窗口、归一与「该源没有这一列」 ---------------------------------------------


def test_the_flow_window_is_the_last_five_trading_days(db):
    """`main_net_in` 按天数合计：把窗口拉到 10 天会让分数翻倍，而那是一段没被问过的日子。

    5 天 × 5 亿元 ÷ 市值 6000 亿 = 0.4167%，落在 `FLOW_SCORE_ANCHORS` 的 0 与 1% 之间。
    """
    read = build(db, flow=flow_rows(DAYS[-10:], net=5e8)).read("A", CODE)
    ratio = 5 * 5e8 / MARKET_CAP
    assert [str(d) for d in read.flow_days] == [str(d) for d in DAYS[-5:]]
    assert item(read, "main_inflow").value == pytest.approx(50.0 + ratio / 0.01 * 50.0)


def test_a_flow_column_with_no_values_refuses_that_item_instead_of_scoring_zero(db):
    """北向那一列现在没有源给得出（U25）：合计「空」不是 0 元净流入。"""
    read = build(db, flow=flow_rows(DAYS[-5:], net=5e8, north=None)).read("A", CODE)
    assert item(read, "main_inflow").ok
    assert not item(read, "north_bound").ok
    assert "没有数" in item(read, "north_bound").reason


def test_no_flow_rows_at_all_leaves_both_funding_items_missing(db):
    """一行都没有 ⇒ 两路都是「没数据」，且下一句给的是 `sams sync flow`。

    与上面那条的分界在这里：那一例是「有行、这一列全空」（该源不给这一列，U25），
    这一例是「这几天我们根本没同步过」——两者的下一步命令不同。
    """
    read = build(db).read("A", CODE)
    assert not item(read, "main_inflow").ok
    assert not item(read, "north_bound").ok
    assert {"main_inflow", "north_bound"} <= set(read.composite.missing)
    assert "sams sync flow" in item(read, "main_inflow").reason
    assert read.flow_days, "问了哪几天要能对着那几天看"


# --- 「库里没有」要说得下去下一步命令 -----------------------------------------------------


def test_a_market_with_no_fundamental_rows_refuses_and_names_the_next_command(db):
    """港美股在库里没有基本面行：屏幕上要有一句下一步，而不是一个恰好很低的分（D43）。"""
    service = build(db, market="HK", code="00700", with_fundamentals=False)
    read = service.read("HK", "700")
    assert (read.market, read.code) == ("HK", "00700")
    assert not read.composite.ok
    assert read.composite.covered_weight < 0.5, "只有技术面那 23% 拿到了数，过不了覆盖地板"
    assert "sams sync" in read.composite.reason
    assert "sams sync valuation" in reading(read, "pr").reason
    assert not read.plan.ok
    assert "sams analysis fundamentals" in read.plan.fair.reason


def test_the_style_travels_with_the_read(db):
    """合成分旁边那句权重话要点名是**哪套**模板：同一只票两套模板给两个分是正常的。"""
    read = build(db).read("A", CODE, style="trend")
    assert read.style is TREND
    assert "模板 trend" in read.composite.evidence
