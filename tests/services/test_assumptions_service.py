"""证伪台账的用例层（D232 P0-3）。

一条假设是「指标 + 方向 + 阈值 + 复核日」；判一次就是拿截至那天的基本面读数比一下。
这一层要钉住三件事：

* 判的是**那一天的库**，不是今天重算出来的数（同 D61 的 PIT 纪律）；
* 数据不够时是「数据不足」，不是「证伪」——两者在屏幕上都会让人想动手；
* 别人的 thesis 连假设都读不到（归属校验在仓库层，这里只验接线没漏）。
"""

from __future__ import annotations

import pytest
from support_valuation import CODE, TODAY, make_container

from sams.core.assumptions import MET, OPEN, REFUTED, UNKNOWN
from sams.core.errors import StorageError, ValidationError

USER = "trader"
OTHER = "watcher"
PWD = "correct-horse-battery"
#: 样本库里没有这只票的日线与财报：它的读数全是 None
NO_DATA_CODE = "600000"


def build(db, settings):
    container = make_container(db, settings)
    container.users.create(USER, PWD)
    container.users.create(OTHER, PWD)
    thesis = container.theses.add(USER, "A", CODE).thesis
    return container, thesis


def test_an_assumption_starts_open_and_keeps_its_review_day(db, settings) -> None:
    container, thesis = build(db, settings)
    saved = container.assumptions.add(
        USER,
        thesis.id,
        metric="roe",
        op=">=",
        threshold=0.15,
        due_on=TODAY,
        note="ROE 不能掉下 15%",
    )
    assert saved.status == OPEN
    assert saved.due_on == TODAY
    listed = container.assumptions.list(USER, thesis.id)
    assert [item.id for item in listed] == [saved.id]
    assert listed[0].note == "ROE 不能掉下 15%"


def test_an_unknown_metric_is_refused_at_the_door(db, settings) -> None:
    container, thesis = build(db, settings)
    with pytest.raises(ValidationError):
        container.assumptions.add(USER, thesis.id, metric="pe", op=">=", threshold=10.0)
    with pytest.raises(ValidationError):
        container.assumptions.add(USER, thesis.id, metric="roe", op="=", threshold=0.1)


def test_evaluation_uses_the_reading_of_that_day_and_records_it(db, settings) -> None:
    container, thesis = build(db, settings)
    container.assumptions.add(USER, thesis.id, metric="roe", op=">=", threshold=0.15)
    (checked,) = container.assumptions.evaluate(USER, thesis.id)
    assert checked.status == MET
    assert checked.evaluated_value == pytest.approx(0.20)
    assert checked.evaluated_on == TODAY


def test_a_failed_comparison_is_refuted_not_unknown(db, settings) -> None:
    container, thesis = build(db, settings)
    container.assumptions.add(USER, thesis.id, metric="roe", op=">=", threshold=0.25)
    (checked,) = container.assumptions.evaluate(USER, thesis.id)
    assert checked.status == REFUTED


def test_missing_data_is_unknown_not_refuted(db, settings) -> None:
    container = make_container(db, settings)
    container.users.create(USER, PWD)
    thesis = container.theses.add(USER, "A", NO_DATA_CODE).thesis
    container.assumptions.add(USER, thesis.id, metric="roe", op=">=", threshold=0.15)
    (checked,) = container.assumptions.evaluate(USER, thesis.id)
    assert checked.status == UNKNOWN
    assert checked.evaluated_value is None


def test_another_user_cannot_read_or_judge_the_assumptions(db, settings) -> None:
    container, thesis = build(db, settings)
    saved = container.assumptions.add(USER, thesis.id, metric="roe", op=">=", threshold=0.15)
    with pytest.raises(StorageError):
        container.assumptions.list(OTHER, thesis.id)
    with pytest.raises(StorageError):
        container.assumptions.evaluate(OTHER, thesis.id)
    with pytest.raises(StorageError):
        container.assumptions.remove(OTHER, thesis.id, saved.id)


def test_remove_takes_only_that_row(db, settings) -> None:
    container, thesis = build(db, settings)
    first = container.assumptions.add(USER, thesis.id, metric="roe", op=">=", threshold=0.15)
    second = container.assumptions.add(
        USER, thesis.id, metric="dividend_yield", op=">=", threshold=0.03
    )
    container.assumptions.remove(USER, thesis.id, first.id)
    assert [item.id for item in container.assumptions.list(USER, thesis.id)] == [second.id]


def test_the_daily_sweep_reports_only_the_transition_into_refuted(db, settings) -> None:
    """告警只在「变成证伪」那一次发：每天都发一遍，收件箱会先教会人忽略它。"""
    container, thesis = build(db, settings)
    container.assumptions.add(USER, thesis.id, metric="roe", op=">=", threshold=0.25)
    first = container.assumptions.evaluate_user(USER)
    assert first.checked == 1
    assert [(hit.thesis_id, hit.metric, hit.value) for hit in first.refuted] == [
        (thesis.id, "roe", pytest.approx(0.20))
    ]
    again = container.assumptions.evaluate_user(USER)
    assert again.checked == 1
    assert again.refuted == ()
    assert container.assumptions.list(USER, thesis.id)[0].status == REFUTED


def test_the_daily_sweep_stays_within_the_job_market(db, settings) -> None:
    container, thesis = build(db, settings)
    container.assumptions.add(USER, thesis.id, metric="roe", op=">=", threshold=0.25)
    assert container.assumptions.evaluate_user(USER, market="HK").checked == 0
    assert container.assumptions.evaluate_user(USER, market="A").checked == 1


def test_missing_data_is_counted_but_never_reported_as_refuted(db, settings) -> None:
    container = make_container(db, settings)
    container.users.create(USER, PWD)
    thesis = container.theses.add(USER, "A", NO_DATA_CODE).thesis
    container.assumptions.add(USER, thesis.id, metric="roe", op=">=", threshold=0.15)
    report = container.assumptions.evaluate_user(USER)
    assert report.checked == 1
    assert report.refuted == ()
