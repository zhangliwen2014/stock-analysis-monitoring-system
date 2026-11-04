"""论点详情页的证伪台账（D232 P0-3）：加一条、判一次、删一条。

页面不自己算比较：判定与读数都来自 `AssumptionService.evaluate`，
这里只验接线与屏幕上那几句话。
"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl


def _thesis(logged) -> str:
    return logged.context.theses.add("alice", "A", "600519", note="高端酒").thesis.id


def _seed_roe(logged, value=0.20) -> None:
    ctx = logged.context
    today = ctx.clock.today()
    ctx.financials_store.upsert(
        pl.DataFrame(
            {
                "market": ["A"],
                "code": ["600519"],
                "report_date": [date(today.year - 1, 12, 31)],
                "published_date": pl.Series(
                    "published_date", [today - timedelta(days=10)], dtype=pl.Date
                ),
                "roe": [value],
                "source": ["akshare"],
            }
        )
    )


def _add(logged, thesis_id: str, *, metric="roe", op=">=", threshold="0.15", due_on="", note=""):
    return logged.client.post(
        f"/theses/{thesis_id}/assumptions",
        data={
            "csrf": logged.csrf,
            "metric": metric,
            "op": op,
            "threshold": threshold,
            "due_on": due_on,
            "note": note,
        },
        follow_redirects=False,
    )


def _evaluate(logged, thesis_id: str):
    return logged.client.post(
        f"/theses/{thesis_id}/assumptions/evaluate",
        data={"csrf": logged.csrf},
        follow_redirects=False,
    )


def test_an_assumption_is_added_and_shown_as_pending(logged) -> None:
    thesis_id = _thesis(logged)
    response = _add(logged, thesis_id, due_on="2026-12-31", note="ROE 不能掉下 15%")
    assert response.status_code == 303
    page = logged.client.get(f"/theses/{thesis_id}").text
    assert "净资产收益率" in page
    assert "15.00%" in page
    assert "2026-12-31" in page
    assert "ROE 不能掉下 15%" in page
    assert "待验证" in page


def test_judging_once_records_met_with_the_reading(logged) -> None:
    thesis_id = _thesis(logged)
    _seed_roe(logged, 0.20)
    _add(logged, thesis_id)
    assert _evaluate(logged, thesis_id).status_code == 303
    page = logged.client.get(f"/theses/{thesis_id}").text
    assert "成立" in page
    assert "20.00%" in page


def test_a_broken_assumption_reads_refuted(logged) -> None:
    thesis_id = _thesis(logged)
    _seed_roe(logged, 0.10)
    _add(logged, thesis_id, threshold="0.15")
    _evaluate(logged, thesis_id)
    page = logged.client.get(f"/theses/{thesis_id}").text
    assert "已证伪" in page
    assert "10.00%" in page


def test_missing_data_reads_not_judged(logged) -> None:
    thesis_id = _thesis(logged)
    _add(logged, thesis_id)
    _evaluate(logged, thesis_id)
    page = logged.client.get(f"/theses/{thesis_id}").text
    assert "数据不足" in page


def test_delete_removes_only_the_named_assumption(logged) -> None:
    thesis_id = _thesis(logged)
    _add(logged, thesis_id, note="要删的")
    _add(logged, thesis_id, metric="pb", op="<=", threshold="5", note="留着的")
    rows = logged.context.assumptions.list("alice", thesis_id)
    # 同一次冻结时钟下 created_at 相同，顺序只能按 id 兜底——这里按内容挑，不按位置
    kept = next(item for item in rows if item.note == "留着的")
    dropped = next(item for item in rows if item.note == "要删的")
    response = logged.client.post(
        f"/theses/{thesis_id}/assumptions/{dropped.id}/delete",
        data={"csrf": logged.csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = logged.client.get(f"/theses/{thesis_id}").text
    assert "留着的" in page
    assert "要删的" not in page
    assert [item.id for item in logged.context.assumptions.list("alice", thesis_id)] == [
        kept.id
    ]


def test_a_non_numeric_threshold_is_rejected_with_a_sentence(logged) -> None:
    thesis_id = _thesis(logged)
    response = _add(logged, thesis_id, threshold="abc")
    assert response.status_code == 400
    assert "不是一个数" in response.text
