"""对抗性测试（M2-C 第二道闸 · 服务层）：universe 诚实、粒度守卫、`strategy_id` 存档。

写法与本仓库既有的 adversarial 文件一致：每个用例只证一件事，
`test_should_*` 断言的是「按 spec 必须成立、当场可能不成立」的那条不变式——它**允许是红的**，
红的那一条就是 finding，实现者不许把断言改软来让它变绿。全部离线，内存库。

攻击面：
* D64 universe 诚实：任何一只「已同步基本面」的票都得落在 hits / priced_out / excluded
  三堆里，「我们没数据」不能长成「市场不便宜」，也不能长成「压根没同步」。
* D61 PIT 计数：`dropped_unpublished` 只能数「因 NULL 披露日未被采信」的行，
  按 universe 内的 codes 计——一只只在 universe 外的票的 NULL 行于是彻底隐形。
* D60 粒度守卫是**双向**的：`FinancialRepo.upsert` 拒 `pe_ttm`/`pb`，`ValuationRepo` 拒
  `roe`/`revenue_yoy`/`profit_yoy`；两侧对任何未登记列都当场 `StorageError` 而不是 `select` 丢列。
* D57 存档身份：`strategy_id` 是 (日期, 市场, id) 覆盖写的主键——漏带任何一个「会改变
  篮子」的输入，两次扫描就会互相覆盖，屏幕上「今天扫出什么」变成「最后一次扫出什么」。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import polars as pl
import pytest
from support import FrozenClock

from sams.core.errors import StorageError, ValidationError
from sams.services.screen import ScreenService
from sams.storage.repos.financials import FinancialRepo, ValuationRepo
from sams.storage.repos.scan_results import ScanResultRepo
from sams.storage.repos.symbols import SymbolRepo

FY = date(2023, 12, 31)
FY_PUBLISHED = date(2024, 3, 20)
Q1 = date(2024, 3, 31)
Q1_PUBLISHED = date(2024, 4, 26)
FRESH = date(2024, 7, 5)
AS_OF = date(2024, 7, 9)
#: 早于库里任何一期披露日——用来验「未来函数」：那一期的 ROE 与 PR 必须一起消失
PRE_HISTORY = date(2020, 1, 1)


def screen(db, as_of: date = AS_OF) -> ScreenService:
    return ScreenService(
        FinancialRepo(db),
        ValuationRepo(db),
        SymbolRepo(db),
        ScanResultRepo(db),
        clock=FrozenClock(datetime(as_of.year, as_of.month, as_of.day, 15, tzinfo=UTC)),
    )


def add_financial(
    db,
    code: str,
    *,
    roe: float | None = 0.5,
    report_date: date = FY,
    published: date | None = FY_PUBLISHED,
    market: str = "A",
) -> None:
    FinancialRepo(db).upsert(
        pl.DataFrame(
            [
                {
                    "market": market,
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
    db, code: str, *, pe: float | None = 15.0, pb: float | None = 2.0, on: date = FRESH
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


def add_good(db, code: str, *, pe: float = 15.0, roe: float = 0.5) -> None:
    """一只「能进篮子」的票：年报已披露 + 当日估值齐。PR = pe ÷ roe ÷ 100。"""
    add_financial(db, code, roe=roe)
    add_valuation(db, code, pe=pe)


# ---------- 1. PIT：as_of 前移，被采信那期与 PR 一起消失 ----------


def test_moving_as_of_before_publication_drops_the_adopted_period_and_pr_together(db):
    """D61：ROE 的采信跟着披露日走，不跟着报告期走。as_of 挪到披露日之前 ⇒
    那一期的 ROE 不能被算进 PR，屏幕上也不能出现 PR 那个数。

    那只票**仍在 universe 里**（D64 + D66：库里同步过它），并以一条说得出日期的排除出门。
    """
    add_good(db, "600000", pe=15.0, roe=0.5)  # 年报 FY 披露 FY_PUBLISHED
    # as_of = 披露之后：PR = 15 / 0.5 / 100 = 0.3，进 hits
    after = screen(db).pr_max("A", 0.4, as_of=AS_OF)
    assert [h.code for h in after.hits] == ["600000"]
    assert after.hits[0].value == pytest.approx(0.3)

    # as_of 挪到 FY_PUBLISHED 之前：那期不能被采信
    before = screen(db).pr_max("A", 0.4, as_of=date(2024, 3, 19))
    assert before.hits == [], "披露日之前的 as_of 不能拿那期算 PR"
    assert before.universe_size == 1, "同步过的票必须在范围里说得出，不是从 N 只里消失"
    assert [e.code for e in before.excluded] == ["600000"]
    assert str(FY_PUBLISHED) in before.excluded[0].reason, (
        "排除那句要说出具体的公开日，读者才知道把 --as-of 挪到哪天"
    )
    assert "--as-of" in before.excluded[0].reason, "下一步命令当场可敲"
    assert len(before.hits) + before.priced_out + len(before.excluded) == before.universe_size


def test_as_of_before_every_report_refuses_and_does_not_fall_back(db):
    """as_of 早于库里所有披露日 ⇒ 没有任何 PR 可算，且**绝不落回**某期「更早但更晚披露」
    的数据（那是未来函数）。落回与否看 hits，范围诚实与否看 universe 那句。"""
    add_good(db, "600000")
    scan = screen(db).pr_max("A", 0.4, as_of=PRE_HISTORY)
    assert scan.hits == [] and scan.priced_out == 0, "那一天之前没有任何一期存在"
    assert scan.universe_size == 1, (
        "universe 是「库里同步过基本面的票」，不是「截至那天有可用期的票」——"
        "后者会让一只同步过的票从范围那句话里静默消失（D64）"
    )
    assert [e.code for e in scan.excluded] == ["600000"]


# ---------- 2. dropped_unpublished 的可见范围：universe 外的票彻底隐形 ----------


def test_synced_codes_with_no_publication_date_are_in_the_universe_and_named(db):
    """D64 + D66 的收口：任何一只「库里同步过基本面」的票都得落在三堆里。

    原来 universe 只数「有已披露一期」的票，于是一只披露日全为 NULL 的票既不在 universe、
    也不在 `dropped_unpublished`（那一计数按 universe 内的 codes 问）、也不在 excluded——
    屏幕上「已同步基本面的 1 只」是谎话，而「我们没数据」长得像「压根没同步」。
    """
    add_good(db, "600000", pe=15.0, roe=0.5)
    add_financial(db, "600001", roe=0.4, published=None)  # 只在 financials 里，从没披露
    scan = screen(db).pr_max("A", 0.4)
    assert scan.universe_size == 2, "同步过就要被扫到，哪怕一期都没披露"
    assert scan.dropped_unpublished >= 1, "那一个 NULL 行必须被数出来一次"
    assert [e.code for e in scan.excluded] == ["600001"]
    assert "披露日" in scan.excluded[0].reason, "说的是「没披露日」，不是「只披露到季报」"
    assert "sync financials" in scan.excluded[0].reason, "下一步命令要当场能跑"
    # 三堆覆盖 universe 的每一只
    assert len(scan.hits) + scan.priced_out + len(scan.excluded) == scan.universe_size


def test_a_requested_code_with_no_financial_row_at_all_is_named(db):
    """`--codes` 里写了一只库里没有的票：静默少扫一只，与「这只票不便宜」是两件事。"""
    add_good(db, "600000", pe=15.0, roe=0.5)
    scan = screen(db).pr_max("A", 0.4, codes=["600000", "600999"])
    assert [h.code for h in scan.hits] == ["600000"]
    assert scan.unmatched == ["600999"], "库里没有这一行的代码要出门，不是被交集掉"


def test_dropped_unpublished_only_counts_the_basis_it_was_asked_about(db):
    """刻画现状：`load_as_of(basis=annual)` 只数落在年报口径里的 NULL 行。
    一期的季报 NULL 不进这个数——它在 annual 口径下本来就不参与采信。"""
    add_good(db, "600000", pe=15.0, roe=0.5)
    # 只在**季报口径**下 NULL 披露日：annual 口径的 clause 会把它过滤掉，dropped 不含它
    add_financial(db, "600000", roe=0.1, report_date=Q1, published=None)
    scan = screen(db).pr_max("A", 0.4)
    assert scan.dropped_unpublished == 0, (
        "现状：annual 口径下的 dropped 只数落在年报那一条 clause 里的 NULL 行"
    )


def test_dropped_count_is_the_null_published_rows_not_the_future_published(db):
    """D61 那句「NULL 披露日的行 excluded AND counted」——只覆盖 NULL。
    刻画：`published_date > as_of`（未来的披露）被排除，但不计入 `dropped_unpublished`。
    读者从这句话里读到「0 行未被采信」，会以为库里没有更多待披露的期。"""
    add_financial(db, "600000", roe=0.5)
    add_valuation(db, "600000", pe=15.0)
    # 一份更新但 as_of 之后才披露的年报
    add_financial(
        db, "600000", roe=0.9, report_date=date(2024, 12, 31), published=date(2025, 6, 1)
    )
    scan = screen(db).pr_max("A", 0.4, as_of=AS_OF)
    # 那只票仍进 hits——用的是 FY 那份已披露的年报
    assert [h.code for h in scan.hits] == ["600000"]
    assert scan.hits[0].roe_used == pytest.approx(0.5), "as_of 之后披露的那期不能被采信"
    assert scan.dropped_unpublished == 0, (
        "现状：`dropped_unpublished` 只统计 NULL 的行；「as_of 之后才披露」不出现在这句话里。"
        "屏幕上的『另有 N 行没有披露日』在这种情况下是 0——读者不会知道有一份更新期在 as_of 之后"
    )


# ---------- 3. universe 三堆：hits / priced_out / excluded 不许被合流 ----------


def test_interim_only_stock_appears_in_universe_and_excluded_not_silently_dropped(db):
    """D64：一只只有季报的票在 universe（「有已披露一期」），年报口径下算不出 PR
    ⇒ 进 excluded 点名。不能让它从「已同步基本面的 N 只」那句话里静默消失。"""
    add_financial(db, "600000", roe=0.5, report_date=Q1, published=Q1_PUBLISHED)
    add_valuation(db, "600000", pe=15.0)
    scan = screen(db).pr_max("A", 0.4)
    assert scan.universe_size == 1, "那只票确实在 universe 里"
    assert [e.code for e in scan.excluded] == ["600000"], "annual 口径下算不出 ⇒ 进 excluded"
    assert "roe-basis latest" in scan.excluded[0].reason, "下一步命令要真的能让它进结果"


def test_valuation_row_missing_goes_to_excluded_not_to_a_missing_universe_slot(db):
    add_financial(db, "600000", roe=0.5)  # 只同步 financials，没同步 valuation
    scan = screen(db).pr_max("A", 0.4)
    assert scan.universe_size == 1
    assert [e.code for e in scan.excluded] == ["600000"]
    assert "sams sync valuation" in scan.excluded[0].reason


def test_priced_out_and_excluded_are_never_merged(db):
    """三堆各自的语义不能混：priced_out 是「算得出 PR、只是贵」，
    excluded 是「连 PR 都算不出」——设计文档 §8.2 明说这是两件事。"""
    add_good(db, "600000", pe=100.0, roe=0.5)  # PR = 2.0：贵，算得出
    add_financial(db, "600001", roe=0.5, report_date=Q1, published=Q1_PUBLISHED)
    add_valuation(db, "600001", pe=15.0)  # annual 口径下算不出
    scan = screen(db).pr_max("A", 0.4)
    assert scan.hits == []
    assert scan.priced_out == 1, "600000 是 priced_out：它算得出，只是贵"
    assert [e.code for e in scan.excluded] == ["600001"], "600001 是 excluded：它算不出"
    # 关键不变式：三堆的和 == universe_size（一只票不会同时出现在两堆里）
    assert len(scan.hits) + scan.priced_out + len(scan.excluded) == scan.universe_size, (
        "hits + priced_out + excluded 必须覆盖 universe 的每一只，一不多一不少"
    )


def test_stale_valuation_lands_in_excluded_not_in_hits(db):
    """D64 明写：按日估值旧过 7 天的票进 excluded，不进 hits——那一篮子不能拿旧 PE 冒充今天。"""
    add_financial(db, "600000", roe=0.5)
    add_valuation(db, "600000", pe=15.0, on=date(2024, 1, 5))  # 旧过 7 天
    scan = screen(db).pr_max("A", 0.4)
    assert scan.hits == []
    assert [e.code for e in scan.excluded] == ["600000"]
    assert "2024-01-05" in scan.excluded[0].reason


# ---------- 4. 粒度守卫：仓库层对「借来的列」的处理 ----------


def test_financial_repo_rejects_pe_ttm_and_pb(db):
    """D60 的机器守卫：这两列不属于报告期那张表；当场抛 StorageError，不能静默丢列。"""
    with pytest.raises(StorageError):
        FinancialRepo(db).upsert(
            pl.DataFrame(
                [
                    {
                        "market": "A",
                        "code": "600000",
                        "report_date": FY,
                        "published_date": FY_PUBLISHED,
                        "roe": 0.5,
                        "pe_ttm": 15.0,  # 借来的列
                        "source": "akshare",
                    }
                ],
                schema={
                    "market": pl.Utf8,
                    "code": pl.Utf8,
                    "report_date": pl.Date,
                    "published_date": pl.Date,
                    "roe": pl.Float64,
                    "pe_ttm": pl.Float64,
                    "source": pl.Utf8,
                },
            )
        )


def test_roe_written_to_valuation_daily_is_refused(db):
    """D60 是双向的：`roe` 属于报告期，写进 `valuation_daily` 时也必须当场拒。

    少了这一层时 `_prepare` 直接 `select(columns)` 把列丢掉——调用方以为写进去了，
    读的时候没有。这一条与 `test_financial_repo_rejects_pe_ttm_and_pb` 合起来才完整。
    """
    with pytest.raises(StorageError):
        ValuationRepo(db).upsert(
            pl.DataFrame(
                [
                    {
                        "market": "A",
                        "code": "600000",
                        "date": FRESH,
                        "pe_ttm": 15.0,
                        "pb": 2.0,
                        "roe": 0.5,  # 借来的列：ROE 属于报告期，不属于交易日
                        "source": "akshare",
                    }
                ]
            )
        )
    # 没有这一层时那行会被写进 valuation_daily 而 `roe` 被静默丢掉
    stored = db.query("SELECT * FROM valuation_daily WHERE code = '600000'")
    assert stored.is_empty(), (
        f"拒了列就不能留下那一行：valuation_daily 现在有 {stored.height} 行"
    )


def test_an_unknown_column_is_refused_not_silently_dropped(db):
    """`FinancialRepo` 原来只对 `pe_ttm`/`pb` 两个名字当场拒，其它未登记列由
    `_prepare` 的 `select(list(columns))` 静默丢弃。

    后果：加一条 `revenue_abs` 的采集，写库不报错、读回来没有——「静默少一列」比
    一次 StorageError 贵。
    """
    with pytest.raises(StorageError):
        FinancialRepo(db).upsert(
            pl.DataFrame(
                [
                    {
                        "market": "A",
                        "code": "600000",
                        "report_date": FY,
                        "published_date": FY_PUBLISHED,
                        "roe": 0.5,
                        "somerandom_extra": 123.4,  # 不在词表里
                        "source": "akshare",
                    }
                ]
            )
        )
    # 没有这一层时那行被写入、那一列被丢
    stored = db.query("SELECT * FROM financials WHERE code = '600000'")
    assert stored.is_empty(), f"未知列被静默丢掉了行：financials 里现在有 {stored.height} 行"


# ---------- 5. strategy_id：会改变篮子的每一个输入都得进 id ----------


def test_different_thresholds_get_different_strategy_ids(db):
    add_good(db, "600000", pe=15.0, roe=0.5)
    a = screen(db).pr_max("A", 0.4)
    b = screen(db).pr_max("A", 0.2)
    assert a.strategy_id != b.strategy_id


def test_different_roe_basis_get_different_strategy_ids(db):
    add_good(db, "600000", pe=15.0, roe=0.5)
    a = screen(db).pr_max("A", 0.4, roe_basis="annual")
    b = screen(db).pr_max("A", 0.4, roe_basis="latest")
    assert a.strategy_id != b.strategy_id


def test_a_codes_subset_scan_is_not_archived_at_all(db):
    """`--codes` 是一次试跑，不是「今天这一篮子」：存档按规则身份，子集根本不进存档。

    契约（D57 + D64）：`scan_results` 的主键是 (日期, 市场, 规则身份)，而规则身份里
    **不该**有 codes——把 codes 编进 id 会让「今天这一篮子」变成 N 行试跑，M3 的通知层
    从此不知道读哪一行。所以子集扫描不存档，并且当场说出来。
    """
    add_good(db, "600000", pe=15.0, roe=0.5)  # PR = 0.3
    add_good(db, "600001", pe=10.0, roe=0.5)  # PR = 0.2
    svc = screen(db)
    full = svc.pr_max("A", 0.4)
    assert full.archived is True, "全量扫描才配占「今天这一篮子」那一行"
    subset = svc.pr_max("A", 0.4, codes=["600000"])
    assert subset.archived is False, "子集扫描不该写存档：它覆盖的是全量那一条"
    record = ScanResultRepo(db).get(on=AS_OF, market="A", strategy_id=full.strategy_id)
    assert [h["code"] for h in record["hits"]] == ["600001", "600000"], (
        f"全量那一次必须还读得回来；现状被试跑压成 {record['hits']}"
    )
    assert record["universe_size"] == 2, record


def test_a_scan_asking_for_no_record_is_not_archived(db):
    """`record=False` 是调用方明确不要存档；它也得能从 `archived` 上读出来（同一句话）。"""
    add_good(db, "600000", pe=15.0, roe=0.5)
    scan = screen(db).pr_max("A", 0.4, record=False)
    assert scan.archived is False
    assert ScanResultRepo(db).count() == 0


def test_an_as_of_after_today_is_refused(db):
    """选股按「截至那天已披露」采信报表：截至日在未来 ⇒ 库里每一期都已披露（D9 的红线）。

    那不是一个「更宽的窗口」，而是一次未来函数：今天的库里有 2026 年报，
    `--as-of 2027-01-01` 就用它算出一个今天无法核实的 PR，而屏幕上仍写着 PIT。
    """
    add_good(db, "600000", pe=15.0, roe=0.5)
    with pytest.raises(ValidationError, match="未来"):
        screen(db).pr_max("A", 0.4, as_of=date(2024, 7, 10))


def test_should_not_overwrite_previous_run_when_include_pb_branch_changes(db):
    """刻画现状：`include_pb_branch` **是**在 id 里的。这条断言应当为绿，
    写在这里是为了与上一条形成对照——id 覆盖了 threshold / roe_basis / include_pb_branch
    三个输入，独独漏了 codes。"""
    add_good(db, "600000", pe=15.0, roe=0.5)
    a = screen(db).pr_max("A", 0.4)
    b = screen(db).pr_max("A", 0.4, include_pb_branch=True)
    assert a.strategy_id != b.strategy_id, "include_pb_branch 已进 id，这条应当绿"


# ---------- 6. 归档里的 hits_json：口径与两个日期跟每一行走 ----------


def test_the_arched_hits_still_carry_basis_period_and_valuation_date(db):
    add_good(db, "600000", pe=15.0, roe=0.5)
    svc = screen(db)
    scan = svc.pr_max("A", 0.4)
    stored = ScanResultRepo(db).get(on=AS_OF, market="A", strategy_id=scan.strategy_id)
    assert stored is not None
    row = stored["hits"][0]
    assert row["basis"] == "pe"
    assert row["period"] == FY or str(row["period"]) == str(FY)
    assert row["valuation_on"] == FRESH or str(row["valuation_on"]) == str(FRESH)
    assert row["estimated"] is False
