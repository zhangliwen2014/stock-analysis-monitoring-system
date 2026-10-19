"""按市赚率选股：批量配对、门槛比较、以及「哪些票被排除了、为什么」。

这里最贵的一种错不是算错，是**静默少一只**。`PR ≤ 0.4` 想找的正是困境反转与周期底部
的票，而那一类票恰恰最容易没有 PR（PE 为负、只报了季报、估值行停在两个月前）。
把它们静默丢掉，选出来的那一篮子看起来完整，实则把规则最该抓的东西筛掉了。
所以每一只没进结果的票都要点名（D43 的「查过且没有」与「没查」在选股里同样是两件事）。

另一半诚实话是 universe：库里同步了基本面的只有 137 只，而 A 股有 5000 多只。
第一行不说出这个边界，「今天没有票满足 PR≤0.4」就会被读成「市场不便宜」。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import polars as pl
import pytest
from support import FrozenClock

from sams.core.fundamentals import BASIS_PB_ESTIMATE, BASIS_PE
from sams.services.screen import ScreenService
from sams.storage.repos.financials import FinancialRepo, ValuationRepo
from sams.storage.repos.scan_results import ScanResultRepo
from sams.storage.repos.symbols import SymbolRepo

FY = date(2023, 12, 31)
#: 披露日在截至日之前：所有期都算「已经公开」，本文件的用例不测 PIT（那在 read 与仓库层）
FY_PUBLISHED = date(2024, 3, 20)
FRESH = date(2024, 7, 5)
STALE = date(2024, 1, 5)
TODAY = date(2024, 7, 9)


def screen(db) -> ScreenService:
    return ScreenService(
        FinancialRepo(db),
        ValuationRepo(db),
        SymbolRepo(db),
        ScanResultRepo(db),
        clock=FrozenClock(datetime(2024, 7, 9, 15, tzinfo=UTC)),
    )


def add_financial(
    db,
    code: str,
    *,
    roe: float | None = 0.2,
    report_date: date = FY,
    published: date | None = FY_PUBLISHED,
) -> None:
    FinancialRepo(db).upsert(
        # 显式 schema：`published=None` 的那一行会让 polars 把整列推成 Null 类型，
        # 而仓库层对「日期列不是 pl.Date」是当场拒绝（那里挡的是带时刻的 datetime）
        pl.DataFrame(
            [
                {
                    "market": "A",
                    "code": code,
                    "report_date": report_date,
                    "published_date": published,
                    "roe": roe,
                    "source": "akshare",
                }
            ],
            schema={
                "market": pl.Utf8,
                "code": pl.Utf8,
                "report_date": pl.Date,
                "published_date": pl.Date,
                "roe": pl.Float64,
                "source": pl.Utf8,
            },
        )
    )


def add_valuation(
    db, code: str, *, pe: float | None = 20.0, pb: float | None = 2.0, on: date = FRESH
) -> None:
    ValuationRepo(db).upsert(
        pl.DataFrame(
            [
                {
                    "market": "A",
                    "code": code,
                    "date": on,
                    "pe_ttm": pe,
                    "pb": pb,
                    "source": "akshare",
                }
            ]
        )
    )


# ---------- 门槛 ----------


def test_the_threshold_is_inclusive_and_the_rows_sort_ascending(db):
    """门槛写在命令行上就是要被正好敲中：`PR == 0.4` 入，`0.41` 不入。"""
    # 18 / 0.45 / 100 = 0.4 整；18.4 / 0.45 = 0.40888…
    add_financial(db, "600000", roe=0.45)
    add_valuation(db, "600000", pe=18.0)
    add_financial(db, "600001", roe=0.45)
    add_valuation(db, "600001", pe=18.4)
    add_financial(db, "600002", roe=0.5)
    add_valuation(db, "600002", pe=15.0)  # PR = 0.3
    scan = screen(db).pr_max("A", 0.4)
    assert [r.code for r in scan.hits] == ["600002", "600000"], "升序，且 0.4 这一只必须在"
    assert scan.hits[1].value == pytest.approx(0.4)
    assert scan.priced_out == 1, "600001 算得出 PR、只是高于门槛：那是落选，不是排除"


def test_every_row_carries_the_basis_it_was_computed_on(db):
    add_financial(db, "600000", roe=0.5)
    add_valuation(db, "600000", pe=15.0)
    row = screen(db).pr_max("A", 0.4).hits[0]
    assert row.basis == BASIS_PE
    assert (row.period, row.valuation_on) == (FY, FRESH), "两个粒度跟着每一行走（D60）"
    assert row.roe_used == pytest.approx(0.5) and row.pe_ttm == 15.0


# ---------- 排除要点名 ----------


def test_a_negative_pe_is_excluded_by_default_and_named(db):
    """PE 不为正 ⇒ 走 PB 那一支，而它算的是「扭亏后能赚多少」：默认不混进排序。"""
    add_financial(db, "600000", roe=0.5)
    add_valuation(db, "600000", pe=-8.0, pb=1.0)  # PB 支：1 / 0.5 / 0.5 / 100 = 0.04
    add_financial(db, "600001", roe=0.5)
    add_valuation(db, "600001", pe=15.0)
    scan = screen(db).pr_max("A", 0.4)
    assert [r.code for r in scan.hits] == ["600001"]
    assert [e.code for e in scan.excluded] == ["600000"]
    reason = scan.excluded[0].reason
    assert "不可放在同一篮子" in reason and "--include-pb-branch" in reason
    assert "预估" not in reason, (
        f"`pr_max` 从不传预估 ROE，那句排除不能说它嵌了一个预估：{reason}"
    )

    included = screen(db).pr_max("A", 0.4, include_pb_branch=True)
    assert [r.code for r in included.hits] == ["600001", "600000"], (
        "两支不可比 ⇒ 先按支分组、组内升序，不是一条全局升序"
    )
    assert included.hits[0].basis == BASIS_PE
    assert included.hits[1].basis == BASIS_PB_ESTIMATE
    assert included.pb_branch_count == 1


def test_a_code_with_no_daily_valuation_is_not_silently_dropped(db):
    add_financial(db, "600000", roe=0.5)
    scan = screen(db).pr_max("A", 0.4)
    assert scan.hits == []
    assert scan.excluded[0].reason.startswith("没有按日估值")
    # 那一步命令必须当场能跑：`sams sync valuation` 缺 --market/--code 会直接报错，
    # 于是这句提示会变成一条敲不通的命令
    assert "sams sync valuation --market A --code 600000" in scan.excluded[0].reason


def test_a_stale_valuation_row_is_named_with_its_date(db):
    """停牌两个月的票「有 PE」，但那个 PE 不属于今天：给它的 PR 是一篮子不同日期的数。"""
    add_financial(db, "600000", roe=0.5)
    add_valuation(db, "600000", pe=15.0, on=STALE)
    scan = screen(db).pr_max("A", 0.4)
    assert scan.hits == []
    assert str(STALE) in scan.excluded[0].reason


def test_a_code_with_only_interim_financials_names_the_basis_flag(db):
    """D62 在选股里的落点：那只票有财务数据，只是没有年报——不说就等于静默丢掉。"""
    add_financial(db, "600000", roe=0.5, report_date=date(2024, 3, 31))
    add_valuation(db, "600000", pe=15.0)
    scan = screen(db).pr_max("A", 0.4)
    assert scan.hits == []
    assert "roe-basis latest" in scan.excluded[0].reason
    # 换成最新一期口径，它就进结果——那句下一步命令是真的能跑的
    latest = screen(db).pr_max("A", 0.4, roe_basis="latest")
    assert [r.code for r in latest.hits] == ["600000"]


# ---------- universe 那句诚实话 ----------


def test_the_universe_counts_the_stocks_actually_scanned(db):
    """主档 5000 只、同步了基本面 2 只：这两个数都要出门，否则「没有命中」会被读成「市场贵」。"""
    for code in ("600000", "600001"):
        add_financial(db, code, roe=0.5)
        add_valuation(db, code, pe=15.0)
    SymbolRepo(db).upsert(
        pl.DataFrame(
            [
                {
                    "market": "A",
                    "code": f"60000{i}",
                    "name": f"票{i}",
                    "currency": "CNY",
                    "list_status": "L",
                }
                for i in range(9)
            ]
        )
    )
    scan = screen(db).pr_max("A", 0.4)
    assert (scan.universe_size, scan.market_size) == (2, 9)


def test_the_requested_codes_narrow_the_universe(db):
    add_financial(db, "600000", roe=0.5)
    add_valuation(db, "600000", pe=15.0)
    add_financial(db, "600001", roe=0.5)
    add_valuation(db, "600001", pe=15.0)
    scan = screen(db).pr_max("A", 0.4, codes=["600001"])
    assert scan.universe_size == 1
    assert [r.code for r in scan.hits] == ["600001"]


def test_the_key_spellings_do_not_change_the_answer(db):
    """`a`/`601088` 与 `A`/`601088` 必须给同一个结果（D50）：漏归一的键会让 universe 少一只。"""
    add_financial(db, "601088", roe=0.5)
    add_valuation(db, "601088", pe=15.0)
    assert [r.code for r in screen(db).pr_max("a", 0.4).hits] == ["601088"]


# ---------- 落库与缺省 ----------


def test_the_scan_is_recorded_and_a_rerun_the_same_day_overwrites_it(db):
    add_financial(db, "600000", roe=0.5)
    add_valuation(db, "600000", pe=15.0)
    repo = ScanResultRepo(db)
    first = screen(db).pr_max("A", 0.4)
    second = screen(db).pr_max("A", 0.4)
    assert first.strategy_id == second.strategy_id
    assert repo.count() == 1, "一天里改门槛跑三遍是常态，留三行等于给「今天扫出什么」三个答案"
    stored = repo.get(on=TODAY, market="A", strategy_id=second.strategy_id)
    assert stored["universe_size"] == 1
    assert [h["code"] for h in stored["hits"]] == ["600000"]


def test_an_empty_database_is_an_empty_scan_not_an_error(db):
    scan = screen(db).pr_max("A", 0.4)
    assert (scan.universe_size, scan.hits, scan.excluded) == (0, [], [])
    assert scan.as_of == TODAY, "截至日缺省问注入的时钟，不问真实今天"


def test_periods_without_a_publication_date_are_counted(db):
    """NULL 披露日的那一期不参与采信，也不能当作不存在（D61）。"""
    add_financial(db, "600000", roe=0.5)
    # 缺披露日的恰好是**更新的那一份年报**：它在年报口径的候选里，所以必须被数出来
    add_financial(db, "600000", roe=0.9, report_date=date(2024, 12, 31), published=None)
    add_valuation(db, "600000", pe=15.0)
    scan = screen(db).pr_max("A", 0.4)
    assert scan.dropped_unpublished == 1
    assert scan.hits[0].roe_used == pytest.approx(0.5), "被挡掉的那期没有替 PR 背书"
