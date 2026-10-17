"""读取用例：库里的裸 K 线 + 公司行为 + 覆盖确认 → 过闸的区间涨跌。

这一层存在的理由只有一条：守卫写在 `analysis` 里，而人能敲出来的路径若是
`SELECT close FROM bars_daily` 自己相除，那三道闸一道也不会生效。交易员试用报告
原话是「命令行根本拿不到复权数」——把过闸的路修得比不过闸的路短，才算真的接上。

这里断言的不是因子算术（那在 `tests/analysis/`），而是**取数口径**：
读哪个库、按谁的时钟、用哪个代码形态。错一个方向都会出一个看起来正常的数。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest
from support import FrozenClock

from sams.core.errors import NotAdjustable
from sams.services.analysis import AnalysisService
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.corp_actions import CorpActionRepo
from sams.storage.repos.corp_actions_coverage import CorpActionsCoverageRepo

TODAY = date(2024, 7, 20)
START = date(2024, 7, 1)
END = date(2024, 7, 11)
EX_DATE = date(2024, 7, 8)
CODE = "601088"

#: 601088 那次的真实形状：除息前收 42.00、每股派 2.10 ⇒ 参考价 39.90，当天收 40.40
CLOSES = [41.00, 41.20, 41.40, 41.60, 41.80, 42.00, 42.00, 40.40, 41.00, 41.40, 42.00]


def bar_rows() -> pl.DataFrame:
    days = [START + timedelta(days=i) for i in range(len(CLOSES))]
    return pl.DataFrame(
        {
            "market": ["A"] * len(days),
            "code": [CODE] * len(days),
            "date": days,
            "close": CLOSES,
            "source": ["akshare"] * len(days),
        }
    )


def action_rows(ex_date: date = EX_DATE, cash: float = 2.10) -> pl.DataFrame:
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
            "note": ["10派21.00元(含税)"],
        }
    )


class SpyActionsRepo:
    """只记账的仓库替身：锁住「读除权记录时到底传没传 PIT 上界」。

    少传 `until` 不会让任何一个算数的用例失败——`actions_in_span` 那道保险会把
    窗口外的日期拦下来——它只会在「未来的除权已经进了库」那种票上悄悄多乘一次。
    所以这条口径要单独钉住，而不是靠组合别的数据间接暴露。
    """

    def __init__(self, inner: CorpActionRepo) -> None:
        self.inner = inner
        self.untils: list[date | None] = []

    def list_for(self, market: str, code: str, *, until: date | None = None) -> pl.DataFrame:
        self.untils.append(until)
        return self.inner.list_for(market, code, until=until)

    def upsert_many(self, frame: pl.DataFrame) -> int:
        return self.inner.upsert_many(frame)


def make_service(db, *, today: date = TODAY, actions: CorpActionRepo | None = None):
    """铺一份「K 线齐、还没写覆盖确认」的库，返回服务与两个可继续填的仓库。"""
    BarRepo(db).upsert(bar_rows())
    actions = actions or CorpActionRepo(db)
    coverage = CorpActionsCoverageRepo(db)
    service = AnalysisService(
        BarRepo(db),
        actions,
        coverage,
        clock=FrozenClock(datetime(today.year, today.month, today.day, 15, tzinfo=UTC)),
    )
    return service, actions, coverage


def seeded(db, *, covered_to: date = TODAY, today: date = TODAY, src: str = "akshare", **kw):
    """K 线 + 一条除权记录 + 一段覆盖确认：正常的可出数状态。"""
    service, actions, coverage = make_service(db, today=today, **kw)
    actions.upsert_many(action_rows())
    coverage.record(
        "A",
        CODE,
        covered_from=START,
        covered_to=covered_to,
        full_history=True,
        src=src,
    )
    return service


def get(db, **kwargs):
    args = {"market": "A", "code": CODE, "start": START, "end": END}
    args.update(kwargs)
    return seeded(db).interval_return(**args)


def test_the_number_is_read_from_the_three_repos(db):
    result = get(db)
    assert result.raw == pytest.approx(42.00 / 41.00 - 1, abs=1e-9)
    # 末根乘上因子（42.00/39.90），首根在除权日之前不乘：这才是交易所口径的那段涨跌
    assert result.adjusted == pytest.approx(42.00 * (42.00 / 39.90) / 41.00 - 1, abs=1e-9)
    assert (result.ex_dates, result.covered_by) == (1, "akshare")
    assert result.differs


def test_actions_are_read_as_of_today_not_as_of_the_whole_table(db):
    spy = SpyActionsRepo(CorpActionRepo(db))
    service = seeded(db, actions=spy)
    service.interval_return("A", CODE, START, END)
    assert spy.untils == [TODAY]


def test_a_code_whose_ex_rights_were_never_confirmed_is_refused(db):
    """0 行除权记录不等于没分过红：不许悄悄退回裸价给数（D36）。"""
    service, _, _ = make_service(db)
    with pytest.raises(NotAdjustable, match="覆盖确认"):
        service.interval_return("A", CODE, START, END)


def test_a_code_with_no_bars_at_all_is_refused(db):
    service, _, _ = make_service(db)
    with pytest.raises(NotAdjustable, match="不足两根"):
        service.interval_return("A", "000001", START, END)


def test_an_alias_reads_the_same_stock(db):
    """别名查不到又不报错，读的就是一个空区间：进库和读数必须同一个键形态。"""
    service = seeded(db)
    assert service.interval_return("A", f"sh{CODE}", START, END) == service.interval_return(
        "A", CODE, START, END
    )


def test_the_clock_decides_whether_a_confirmation_is_still_fresh(db):
    """确认做到 7-15 为止，今天已经 7-20：那之后的除权没人看过，拒绝。

    新鲜度只能来自注入的时钟——按请求窗口的末日判新鲜，等于让调用方用一次窄窗口
    买到一张免检票。
    """
    stale = seeded(db, covered_to=date(2024, 7, 15), today=date(2024, 7, 20))
    with pytest.raises(NotAdjustable, match="覆盖确认"):
        stale.interval_return("A", CODE, START, END)
    # 同一个库，把时钟拨回确认那天的下午，同一段区间就该出数
    fresh = seeded(db, covered_to=date(2024, 7, 15), today=date(2024, 7, 15))
    assert fresh.interval_return("A", CODE, START, END).ex_dates == 1


def test_a_window_the_bars_do_not_really_cover_is_refused(db):
    """库里只有 11 天，请求一整年：拒绝，而不是给一个顶着「年度涨跌」名字的窄区间数。"""
    service = seeded(db)
    with pytest.raises(NotAdjustable, match="只覆盖"):
        service.interval_return("A", CODE, START, date(2024, 12, 31))
