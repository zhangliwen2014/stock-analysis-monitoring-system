"""公司行为同步用例：入库 + 覆盖确认（D36）是这一次同步的两件产物，缺一不可。

桩只回答 `fetch_actions` 的结果，把「谁被降级、谁被跳过」留给 registry 自己的测试。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import polars as pl
import pytest
from support import FrozenClock

from sams.core.errors import DatasourceError, StorageError
from sams.datasource.actions import empty_actions
from sams.datasource.registry import ActionsResult, Attempt
from sams.services.corp_actions import CorpActionSyncService
from sams.storage.repos.corp_actions import CorpActionRepo
from sams.storage.repos.corp_actions_coverage import CorpActionsCoverageRepo

TODAY = date(2024, 7, 9)
D1, D2 = date(2024, 1, 2), date(2024, 7, 8)


def actions_frame(source: str = "akshare", *, rows: int = 1, cash: float = 2.26) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "market": ["A"] * rows,
            "code": ["601088"] * rows,
            "ex_date": [D2],
            "kind": ["dividend"],
            "cash_per_share": [cash] * rows,
            "ratio": [None] * rows,
            "price": [None] * rows,
            "src": [source] * rows,
            "note": ["10派22.60元(含税)"] * rows,
        }
    )


class StubRegistry:
    def __init__(self, result: ActionsResult | Exception, *, full_history: bool = True) -> None:
        self.result = result
        self.full_history = full_history
        self.calls: list[tuple] = []

    def fetch_actions(self, market, code, start, end) -> ActionsResult:
        self.calls.append((market, code, start, end))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def make_service(db, result, *, clock: FrozenClock | None = None):
    registry = StubRegistry(result)
    actions, coverage = CorpActionRepo(db), CorpActionsCoverageRepo(db)
    service = CorpActionSyncService(
        registry,
        actions,
        coverage,
        clock=clock or FrozenClock(datetime(2024, 7, 9, 15, tzinfo=UTC)),
    )
    return service, registry, actions, coverage


def ok_result(**kw) -> ActionsResult:
    defaults = dict(
        market="A",
        code="601088",
        frame=actions_frame(),
        source="akshare",
        attempts=(Attempt("akshare", True),),
        full_history=True,
    )
    return ActionsResult(**{**defaults, **kw})


def test_sync_writes_actions_and_a_coverage_claim(db):
    service, _, actions, coverage = make_service(db, ok_result())
    summary = service.sync_actions("A", "601088", D1, D2)
    assert (summary.fetched, summary.written) == (1, 1)
    assert actions.count() == 1
    claim = coverage.get("A", "601088", src="akshare")
    assert claim.full_history is True


def test_running_twice_keeps_row_count_stable(db):
    service, _, actions, coverage = make_service(db, ok_result())
    service.sync_actions("A", "601088", D1, D2)
    service.sync_actions("A", "601088", D1, D2)
    assert actions.count() == 1
    assert coverage.count() == 1


def test_alias_code_lands_on_the_canonical_key(db):
    """别名会把同一只票存成两个主键，覆盖标记也就对不上读取层的查询。"""
    service, registry, actions, coverage = make_service(db, ok_result())
    service.sync_actions("A", "sh601088", D1, D2)
    assert registry.calls[0][1] == "601088"
    assert actions.list_for("A", "601088").height == 1
    assert coverage.get("A", "601088", src="akshare") is not None


def test_an_empty_answer_still_records_coverage(db):
    """「查过且没有」必须与「没查过」分开（D36）：空结果也是这次同步的产物。"""
    service, _, actions, coverage = make_service(db, ok_result(frame=empty_actions()))
    summary = service.sync_actions("A", "601088", D1, D2)
    assert (summary.fetched, summary.written) == (0, 0)
    assert actions.count() == 0
    assert coverage.get("A", "601088", src="akshare").covered_to == TODAY


def test_full_history_claim_is_confirmed_up_to_today_not_the_requested_end(db):
    """源说「历史给全了」是对今天这一天的快照，与请求窗口多窄无关。"""
    service, _, _, coverage = make_service(db, ok_result())
    summary = service.sync_actions("A", "601088", D1, date(2024, 3, 1))
    assert summary.covered_to == TODAY
    assert summary.full_history is True


def test_a_windowed_source_only_confirms_what_it_was_asked(db):
    service, _, _, coverage = make_service(db, ok_result(full_history=False))
    summary = service.sync_actions("A", "601088", D1, D2)
    assert summary.covered_to == D2
    claim = coverage.get("A", "601088", src="akshare")
    assert (claim.full_history, claim.covered_from, claim.covered_to) == (False, D1, D2)


def test_a_future_end_is_clamped_to_today(db):
    """今天的除权公告不该替过去的区间背书，也不该写成「已确认至未来」。"""
    service, registry, _, coverage = make_service(db, ok_result())
    summary = service.sync_actions("A", "601088", D1, date(2025, 1, 1))
    assert registry.calls[0][3] == TODAY
    assert summary.clamped_to == TODAY
    assert coverage.get("A", "601088", src="akshare").covered_to == TODAY


def test_an_entirely_future_window_refuses_without_recording_anything(db):
    service, _, actions, coverage = make_service(db, ok_result())
    with pytest.raises(DatasourceError, match="之后"):
        service.sync_actions("A", "601088", date(2025, 1, 1), date(2025, 6, 1))
    assert (actions.count(), coverage.count()) == (0, 0)


def test_a_failed_fetch_records_no_coverage(db):
    """取数失败时什么都不写：留下一条没根据的确认，比让区间收益拒绝更糟。"""
    service, _, actions, coverage = make_service(
        db, DatasourceError("registry", "所有源均失败")
    )
    with pytest.raises(DatasourceError, match="所有源"):
        service.sync_actions("A", "601088", D1, D2)
    assert (actions.count(), coverage.count()) == (0, 0)


def test_a_source_that_disagrees_about_one_action_writes_no_coverage(db):
    """降级到备源、而备源对同一条记录给了另一个数：这次同步必须整体失败。

    覆盖确认说的是「这段的除权记录齐了」，而此刻连齐不齐都没人知道。静默取一个
    （主键装不下两条）会放行一个看起来正常的错数——交易员闸查出的正是这一条。
    """
    first = ok_result()
    second = ok_result(frame=actions_frame(source="eastmoney", cash=3.0), source="eastmoney")
    registry = StubRegistry(first)
    actions, coverage = CorpActionRepo(db), CorpActionsCoverageRepo(db)
    service = CorpActionSyncService(
        registry, actions, coverage, clock=FrozenClock(datetime(2024, 7, 9, 15, tzinfo=UTC))
    )
    service.sync_actions("A", "601088", D1, D2)
    registry.result = second
    with pytest.raises(StorageError, match="冲突"):
        service.sync_actions("A", "601088", D1, D2)
    assert actions.list_for("A", "601088")["cash_per_share"].to_list() == [2.26], (
        "第二个源不该把已记录的那条覆盖掉"
    )
    assert coverage.get("A", "601088", src="eastmoney") is None, (
        "冲突的那次同步不给 eastmoney 留下任何确认"
    )


def test_degraded_chain_and_skipped_sources_are_reported(db):
    result = ok_result(
        attempts=(Attempt("tencent", False, 1, "无数据"), Attempt("akshare", True)),
        skipped=(("tencent", "不提供公司行为"),),
        full_history=False,
    )
    service, _, _, _ = make_service(db, result)
    summary = service.sync_actions("A", "601088", D1, D2)
    assert summary.degraded is True
    assert summary.skipped == (("tencent", "不提供公司行为"),)
