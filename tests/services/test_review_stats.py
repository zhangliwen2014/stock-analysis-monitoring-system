"""§8.4 复盘口径：纯函数，输入是 `AlertRepo.list_window` 那种原始 dict。

分母为空是 None 不是 0（「没有可关的」与「一条没关上」是两句话）；旧版行不进分布；
撤销后的行按当前状态算——这些全在「读哪一列」的层面，不需要数据库也断言得动。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sams.services.review_stats import MIN_RULE_SAMPLES, summarize, top_rule_ratios

NOW = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)


def _row(**overrides):
    row = {
        "id": "a1",
        "rule_id": "r1",
        "rule_name": "止损线",
        "triggered_at": NOW,
        "status": "new",
        "severity": "mid",
        "acknowledged_at": None,
        "handled_at": None,
        "action_kind": None,
        "action_ref": None,
        "dismiss_code": None,
        "thesis_id": None,
        "deferred_until": None,
        "market": "A",
        "code": "600519",
    }
    row.update(overrides)
    return row


def test_empty_window_has_no_rates():
    stats = summarize([])
    assert stats.total == 0
    assert stats.close_rate is None and stats.ack_rate is None
    assert stats.act_rate is None and stats.dismiss_rate is None
    assert stats.median_close_hours is None


def test_rates_use_new_as_the_denominator():
    rows = [
        _row(id="a1", status="acknowledged", acknowledged_at=NOW),
        _row(
            id="a2",
            status="handled",
            acknowledged_at=NOW,
            handled_at=NOW + timedelta(hours=2),
            action_kind="watch",
        ),
        _row(id="a3", status="new"),
    ]
    stats = summarize(rows)
    assert stats.close_rate == 1 / 3
    assert stats.ack_rate == 2 / 3
    assert stats.act_rate == 1.0 and stats.dismiss_rate == 0.0
    assert stats.action_kinds == {"watch": 1}
    assert stats.median_close_hours == 2.0


def test_legacy_rows_stay_out_of_the_distributions():
    rows = [
        _row(id="a1", status="handled", handled_at=NOW + timedelta(hours=1)),
        _row(id="a2", status="dismissed", handled_at=NOW + timedelta(hours=3)),
    ]
    stats = summarize(rows)
    assert stats.closed == 2  # 状态口径不变：旧版行真的被关掉了
    assert stats.action_kinds == {} and stats.dismiss_codes == {}
    assert stats.legacy_handled == 1 and stats.legacy_dismiss == 1


def test_undo_flattens_back_to_open_by_status():
    """撤销把 status 改回 acknowledged：闭环率读的是当前状态，撤销过就不算关。"""
    rows = [_row(status="acknowledged", acknowledged_at=NOW, handled_at=None)]
    assert summarize(rows).closed == 0


def test_median_of_three_close_spans():
    rows = [
        _row(
            id=f"a{i}",
            status="dismissed",
            handled_at=NOW + timedelta(hours=h),
            dismiss_code="expected",
        )
        for i, h in enumerate((1, 2, 11))
    ]
    assert summarize(rows).median_close_hours == 2.0


def test_rule_ratio_ranks_and_thin_split():
    rows = []
    for rule, fired, missed in (("r1", 5, 4), ("r2", 3, 2), ("r3", 2, 2)):
        for index in range(fired):
            dismissed = index < missed
            rows.append(
                _row(
                    id=f"{rule}-{index}",
                    rule_id=rule,
                    rule_name=f"规则{rule}",
                    status="dismissed" if dismissed else "new",
                    dismiss_code="expected" if dismissed else None,
                    handled_at=NOW + timedelta(hours=1) if dismissed else None,
                )
            )
    ranked, thin = top_rule_ratios(rows)
    assert [x.rule_id for x in ranked] == ["r1", "r2"]
    assert [x.rule_id for x in thin] == ["r3"]
    assert ranked[0].ratio == 4 / 5 and not ranked[0].thin
    assert thin[0].triggered == MIN_RULE_SAMPLES - 1


def test_null_rule_rows_stay_out_of_the_ranking():
    """SYS 系统告警（`rule_id` 为 NULL）不进排名：聚成名为「None」的「规则」
    再链到 `/rules/None/edit` 是幽灵链接——它既不是规则，也没有可编辑的页。
    入库路径见 `monitor/queue.py` 与 `services/scheduler/alarms.py`（rule_id=None）。
    """
    rows = [_row(id=f"s{i}", rule_id=None, rule_name="SYS") for i in range(5)]
    rows.append(_row(id="r1-1", rule_id="r1", status="dismissed", dismiss_code="expected"))
    ranked, thin = top_rule_ratios(rows)
    assert [x.rule_id for x in ranked + thin] == ["r1"]


def test_ranking_is_capped_at_five():
    rows = [
        _row(id=f"{rule}-{index}", rule_id=rule, status="dismissed", dismiss_code="expected")
        for rule in range(8)
        for index in range(MIN_RULE_SAMPLES)
    ]
    ranked, thin = top_rule_ratios(rows)
    assert len(ranked) == 5 and thin == []
