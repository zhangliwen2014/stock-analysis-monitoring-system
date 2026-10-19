"""历史分位：把 PR 摆回它自己的时间序列（M2-D，spec §8.3）。

这条函数只多不少地继承 M2-C 定下的三条口径，而它们每一个的错误症状都是**一个看起来
完全正常的数**：

- **D61 / D9**：某一天能用的 ROE，是那天的**发布日**已经到期的那一期。差这一条就是拿
  T 之后才公开的财务，算 T 那天的分位——历史分位全部由过去的日期组成，所以这条一旦写错，
  错的是整条曲线，而不是一行。
- **D59**：PE 那一支与 PB 那一支算出来的 PR 不可比。把它们放进同一条历史里排，
  分位数的意思变成「和过去的一些数比」，而那些数不是同一种东西。
- **D66**：一个忘了 ÷100 的 ROE 会让那一天所有配了它的 PE 都显得「便宜两个数量级」。

分层：`analysis` 只许 import `core`（`tests/test_layering.py`）。所以这里的输入是**已经
从库里读出来的行**，谁去读、读到哪一天是 services 的事。
"""

from __future__ import annotations

from datetime import date

import pytest

from sams.analysis.valuation.percentile import (
    DEFAULT_WINDOW_YEARS,
    MIN_SAMPLES,
    DisclosedPeriod,
    ValuationDay,
    metric_percentile,
    pr_percentile,
)
from sams.core.fundamentals import ROE_BASIS_ANNUAL, ROE_BASIS_LATEST


def days(*specs: tuple[str, float | None]) -> list[ValuationDay]:
    """按 (日期, PE) 造按日估值行；PB 在这里用不上（历史那一支不许走 PB，见 D59 那组）。"""
    return [ValuationDay(day=date.fromisoformat(d), pe_ttm=pe) for d, pe in specs]


def periods(*specs: tuple[str, float, str | None]) -> list[DisclosedPeriod]:
    """按 (报告期, ROE 小数, 发布日) 造财务行。"""
    return [
        DisclosedPeriod(
            report_date=date.fromisoformat(r),
            roe=roe,
            published_date=date.fromisoformat(p) if p else None,
        )
        for r, roe, p in specs
    ]


def span(count: int, *, end: date = date(2026, 10, 16), pe: float = 20.0) -> list[ValuationDay]:
    """连续 `count` 天、每天同一个 PE。分位要的是「有多少天比现在低」，等值最好评判。"""
    start = end.toordinal() - count + 1
    return [
        ValuationDay(day=date.fromordinal(o), pe_ttm=pe) for o in range(start, end.toordinal() + 1)
    ]


#: 一份「早就公开了的年报」：除专门测披露时序的那几条以外，其余用例都要分母覆盖整段窗口，
#: 否则测的会是「发布日没覆盖到」而不是那条断言本身。
ANNUAL = periods(("2015-12-31", 0.20, "2016-03-31"))


def test_a_period_not_yet_published_on_that_day_is_not_available_on_that_day():
    """D61 逐日版：发布日在历史中间时，那之前的每一天都不许看见这一期。

    只测「截至日之后的不采信」不够——分位是对着一整段历史算的，写入顺序错了会污染整条曲线。
    """
    # 年报 2025（ROE 0.10）在 2026-04-01 才公开，那之前的每一天只能用 2024 年报（0.30）
    rows = days(("2026-03-31", 20.0), ("2026-04-02", 20.0))
    known = periods(("2024-12-31", 0.30, "2025-03-31"), ("2025-12-31", 0.10, "2026-04-01"))
    got = pr_percentile(rows, known, date(2026, 4, 2), window_years=1)
    assert got.current == pytest.approx(20.0 / 0.10 / 100.0), "发布日当天起才用新那一期"

    earlier = pr_percentile(rows, known, date(2026, 3, 31), window_years=1)
    assert earlier.current == pytest.approx(20.0 / 0.30 / 100.0), "2026-03-31 那天新那期还没公开"


def test_the_roe_used_on_a_day_is_the_latest_period_not_the_latest_publication():
    """PIT 的回答必须与 `FinancialRepo.load_as_of` 那条 SQL 是同一个问题、同一个答案。

    仓库侧按「报告期最新」选期（`report_date DESC`），不是按「谁最近被公开」。这里改成按
    发布日选，同一天在两条命令里就会用两个不同的分母——`sams analysis fundamentals` 报
    PR=1.6 而分位曲线按另一个数排，两句在屏幕上都像实话。追溯重述因此**不**覆盖新报告期。
    """
    known = periods(
        ("2025-12-31", 0.30, "2026-03-31"),
        # 2024 年报在 2026-05 被重述过一次；按仓库口径它仍然是较旧的那一期
        ("2024-12-31", 0.10, "2026-05-01"),
    )
    got = pr_percentile(days(("2026-06-01", 20.0)), known, date(2026, 6, 1), window_years=1)
    assert got.current == pytest.approx(20.0 / 0.30 / 100.0)


def test_a_period_without_publication_date_is_never_believed_and_always_counted():
    """D61 的第二半：没有发布日 = 不知道那天的人能不能看见它，因此不采信，且要说数。"""
    known = periods(("2025-12-31", 0.30, None), ("2024-12-31", 0.20, "2025-03-31"))
    got = pr_percentile(days(("2026-06-01", 20.0)), known, date(2026, 6, 1), window_years=1)
    assert got.current == pytest.approx(20.0 / 0.20 / 100.0), "无发布日那期不参与"
    assert got.unpublished_periods == 1


def test_quarterly_periods_are_out_on_the_annual_basis_and_in_on_latest():
    """D62 逐日版：默认只用年报，否则 PR 的分子（十二个月）与分母（三个月）不同周期。"""
    known = periods(("2026-03-31", 0.40, "2026-04-28"), ("2025-12-31", 0.20, "2026-03-31"))
    row = days(("2026-06-01", 20.0))
    annual = pr_percentile(row, known, date(2026, 6, 1), basis=ROE_BASIS_ANNUAL, window_years=1)
    assert annual.current == pytest.approx(20.0 / 0.20 / 100.0)

    latest = pr_percentile(row, known, date(2026, 6, 1), basis=ROE_BASIS_LATEST, window_years=1)
    assert latest.current == pytest.approx(20.0 / 0.40 / 100.0)


def test_days_with_a_non_positive_pe_are_dropped_and_counted_never_treated_as_cheap():
    """D59：PE ≤ 0 的那一天只能走 PB 那一支，而它需要「预估 ROE」——历史里没有预估这回事。

    把它们算进去，分母换了一个定义，那条曲线就不再是「同一个数的历史」。
    把它们静默丢掉，样本数说的和实际排的不是一回事，所以两个数都得出声。
    """
    rows = span(60, end=date(2026, 10, 16), pe=20.0) + [
        ValuationDay(day=date(2026, 10, 17), pe_ttm=-5.0),
        ValuationDay(day=date(2026, 10, 18), pe_ttm=None),
        ValuationDay(day=date(2026, 10, 19), pe_ttm=0.0),
    ]
    known = periods(("2025-12-31", 0.20, "2026-03-31"))
    got = pr_percentile(rows, known, date(2026, 10, 19), window_years=1)
    assert got.skipped_no_pe == 3, "负、零、缺失三种都得被数到，不能只当「没有这一行」"
    assert got.current is None, "今天自己在 PE 那一支上算不出 PR，就没有可比的历史位置可说"
    assert got.reason, "拒绝必须可归属"


def test_a_roe_that_cannot_be_told_apart_from_a_forgotten_divide_by_100_is_dropped():
    """D66 逐日版：一个没换算的 ROE 会让它配上的每一天都显得便宜两个数量级。

    丢的是**天**而不是「期」：一个分母覆盖多少天，就有多少天一起被丢掉。只数期数的话，
    读的人以为脏了一行，实际脏的是整段。
    """
    poisoned = periods(("2015-12-31", 20.0, "2016-03-31"))
    got = pr_percentile(span(300), poisoned, date(2026, 10, 16), window_years=1)
    assert got.skipped_caliber == 300
    assert got.samples == 0 and got.value is None, "整段历史共用这一个分母时，全段一起被丢掉"
    assert "÷100" in got.reason or "口径" in got.reason

    mixed = periods(("2020-12-31", 20.0, "2021-03-31"), ("2022-12-31", 0.20, "2023-03-31"))
    ok = pr_percentile(span(1800), mixed, date(2026, 10, 16), window_years=5)
    assert ok.ok, "脏的那一期只覆盖窗口的前一截时，后面那些天照样可比，不该整条拒绝"
    assert 0 < ok.skipped_caliber < ok.samples


def test_the_current_value_does_not_vote_for_itself():
    """分位是「现在在过去第几档」。把今天算进过去，今天给自己投一票。"""
    lower = span(299, end=date(2026, 10, 15), pe=10.0)
    today = [ValuationDay(day=date(2026, 10, 16), pe_ttm=30.0)]
    known = ANNUAL
    got = pr_percentile(lower + today, known, date(2026, 10, 16), window_years=1)
    assert got.samples == 299, "样本是**早于**截至日的那些天"
    assert got.value == pytest.approx(100.0), "过去的二百九十九天全都比今天便宜 ⇒ 今天在最贵那一档"

    flat = pr_percentile(span(300), known, date(2026, 10, 16), window_years=1)
    assert flat.samples == 299 and flat.value == pytest.approx(100.0), (
        "PE 全程不变时，每一天与今天相等；相等按「不高于」计入分子，"
        "所以一条平的曲线读作 100 而不是 50——这是本函数的口径，写在 evidence 里"
    )


def test_history_shorter_than_the_floor_refuses_instead_of_printing_a_number():
    """样本过少的分位不是「不太可靠的分位」，它是抽样噪声。拒绝出数并说清要几天。"""
    got = pr_percentile(span(MIN_SAMPLES), ANNUAL, date(2026, 10, 16), window_years=5)
    assert got.value is None
    assert str(MIN_SAMPLES) in got.reason
    assert got.samples == MIN_SAMPLES - 1, "拒绝时也要报出实际样本数，读的人才知道差多少"

    just = pr_percentile(span(MIN_SAMPLES + 1), ANNUAL, date(2026, 10, 16), window_years=5)
    assert just.ok


def test_only_the_window_counts_and_the_window_travels_with_the_answer():
    """「过去五年」要说的是窗口两端：窗口外的旧日子不进样本。"""
    old = [ValuationDay(day=date(2015, 1, 5), pe_ttm=1.0)]
    got = pr_percentile(old + span(300), ANNUAL, date(2026, 10, 16), window_years=1)
    assert got.window_from > date(2015, 1, 5), "2015 那根在窗口外"
    assert got.value == pytest.approx(100.0), (
        "窗口外那个 PE=1（PR 极低）若混进来，会把今天的分位从 100 拉到 99.7——"
        "一个看不见的差异，正是窗口必须随数一起出门的理由"
    )
    assert got.window_to == date(2026, 10, 16)


def test_a_day_with_no_published_roe_yet_is_dropped_and_counted():
    """上市初期：有 PE 而没有已披露年报的那几天不是「PR=0」，是「那天没人知道 PR」。"""
    rows = [
        ValuationDay(day=date.fromordinal(o), pe_ttm=20.0)
        for o in range(date(2021, 10, 18).toordinal(), date(2021, 10, 28).toordinal())
    ] + span(300)
    known = periods(("2021-12-31", 0.20, "2022-03-31"))
    got = pr_percentile(rows, known, date(2026, 10, 16), window_years=5)
    assert got.skipped_no_roe == 10
    assert got.samples == 299


def test_default_window_is_five_years_and_is_named_in_the_evidence():
    got = pr_percentile(span(300), ANNUAL, date(2026, 10, 16))
    assert DEFAULT_WINDOW_YEARS == 5
    assert got.window_from.year == 2021, "窗口 = 截至日往前五年"
    for needle in ("valuation_daily", "financials", "pe_ttm", "roe", str(got.samples)):
        assert needle in got.evidence, f"evidence 要能指出来源字段与样本数，缺 {needle}"


def test_metric_percentile_ranks_a_multiple_without_any_fundamental():
    """PE-PB 历史分位（spec §8.3 那一行）不需要分母：倍数自己的历史就是它的参照系。"""
    rows = span(300, end=date(2026, 10, 15), pe=10.0) + [
        ValuationDay(day=date(2026, 10, 16), pe_ttm=40.0, pb=3.0)
    ]
    pe = metric_percentile(rows, "pe_ttm", date(2026, 10, 16), window_years=1)
    assert pe.value == pytest.approx(100.0) and pe.samples == 300

    pb = metric_percentile(rows, "pb", date(2026, 10, 16), window_years=1)
    assert pb.value is None, "PB 只有今天那一个点：样本不够，不是「PB 处在 100 分位」"


def test_metric_percentile_refuses_a_column_that_is_not_a_multiple():
    with pytest.raises(Exception) as exc:
        metric_percentile(span(300), "roe", date(2026, 10, 16))
    assert "roe" in str(exc.value)
