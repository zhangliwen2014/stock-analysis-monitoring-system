"""选股页：范围行、条件编辑、三个数、无数原因、加自选、方案（D180/D182/D184）。"""

from __future__ import annotations

from datetime import UTC, date, datetime

import polars as pl

from sams.datasource.spot import SPOT_SCHEMA
from sams.storage.repos.snapshots import SnapshotRepo
from sams.storage.repos.symbol_list import SymbolListRepo

DAY = date(2026, 10, 28)
TAKEN = datetime(2026, 10, 28, 8, 41, tzinfo=UTC)


def snap(code, *, pe_dyn=5.0, pb=1.0, turnover=1.0):
    return {
        "code": code,
        "name": f"票{code}",
        "quoted_on": DAY,
        "price": 10.0,
        "pct_chg": 1.0,
        "amount": 1e8,
        "turnover_rate": turnover,
        "volume_ratio": 1.0,
        "amplitude": 1.0,
        "pe_ttm": pe_dyn,
        "pe_dyn": pe_dyn,
        "pb": pb,
        "market_cap": 100.0,
        "float_cap": 90.0,
        "is_st": False,
        "source": "tencent",
    }


def seed(context, *rows, codes=None):
    codes = codes or [row["code"] for row in rows]
    SymbolListRepo(context.db).upsert_batch(
        "A",
        [
            {"code": code, "name": code, "py_initials": code, "py_full": code, "currency": "CNY"}
            for code in codes
        ],
    )
    frame = pl.DataFrame(
        [{"market": "A", "taken_at": TAKEN, **row} for row in rows],
        schema={
            "market": pl.Utf8,
            "taken_at": pl.Datetime("us", "UTC"),
            **SPOT_SCHEMA,
        },
    )
    SnapshotRepo(context.db).replace_day(market="A", day=DAY, taken_at=TAKEN, frame=frame)


def default_seed(logged_screen):
    seed(
        logged_screen.context,
        snap("600519", pe_dyn=5.0, turnover=1.0),  # PR 0.25 通过
        snap("000001", pe_dyn=6.0, turnover=3.0),  # PR 0.36 通过
        snap("300750", pe_dyn=10.0, turnover=1.0),  # PR 1.00 不通过
        snap("600000", pe_dyn=-5.0, turnover=1.0),  # 亏损 → 无数
    )


def test_the_range_line_speaks_before_any_condition_is_chosen(logged_screen):
    default_seed(logged_screen)
    response = logged_screen.client.get("/screen", params={"market": "A"})
    assert response.status_code == 200
    assert "快照 4 行" in response.text
    assert "加一条条件" in response.text


def test_a_new_page_has_one_blank_row_and_the_unit_on_every_field(logged_screen):
    response = logged_screen.client.get("/screen", params={"market": "A"})
    assert '<option value="">（选字段）</option>' in response.text
    assert "现值市赚率 PR（小数比值（0.5 = 50%））" in response.text


def test_adding_and_removing_a_row_are_links_that_keep_the_other_rows(logged_screen):
    params = [
        ("market", "A"),
        ("f", "pr_now"),
        ("o", "<="),
        ("v", "0.5"),
        ("f", "turnover_rate"),
        ("o", ">="),
        ("v", "1"),
    ]
    response = logged_screen.client.get("/screen", params=params)
    assert response.status_code == 200
    assert "再加一条" in response.text
    assert "删掉这一条" in response.text


def test_a_market_that_has_no_snapshot_says_so_instead_of_showing_an_empty_table(logged_screen):
    response = logged_screen.client.get("/screen", params={"market": "HK"})
    assert response.status_code == 200
    assert "还没有全市场快照" in response.text


def test_recollect_queues_the_spot_job_and_says_so(logged_screen):
    default_seed(logged_screen)
    response = logged_screen.client.post(
        "/screen/recollect",
        data={"csrf": logged_screen.csrf, "market": "A"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = logged_screen.client.get(response.headers["location"])
    assert "排进下一拍" in page.text
    assert logged_screen.context.scheduler.pending_requests == 1


def _condition_params(**extra):
    pairs = [
        ("market", "A"),
        ("f", "pr_now"),
        ("o", "<="),
        ("v", "0.5"),
    ]
    pairs.extend(extra.items())
    return pairs


def test_every_condition_reports_passed_failed_missing_and_the_hits(logged_screen):
    default_seed(logged_screen)
    response = logged_screen.client.get("/screen", params=_condition_params())
    assert response.status_code == 200
    assert "通过 2 / 不通过 1 / 无数 1" in response.text
    assert "600519" in response.text and "000001" in response.text
    assert "300750" not in response.text


def test_two_conditions_are_anded_and_the_table_carries_both_values(logged_screen):
    default_seed(logged_screen)
    params = _condition_params() + [("f", "turnover_rate"), ("o", ">="), ("v", "2")]
    response = logged_screen.client.get("/screen", params=params)
    assert "000001" in response.text
    assert "600519" not in response.text


def test_a_bad_value_keeps_the_draft_instead_of_losing_what_was_typed(logged_screen):
    response = logged_screen.client.get(
        "/screen",
        params=[("market", "A"), ("f", "pr_now"), ("o", "<="), ("v", "abc")],
    )
    assert response.status_code == 200
    assert "的值要一个数" in response.text
    assert 'value="abc"' in response.text


def test_an_unknown_field_is_refused_with_the_vocabulary(logged_screen):
    response = logged_screen.client.get(
        "/screen",
        params=[("market", "A"), ("f", "nope"), ("o", "<="), ("v", "1")],
    )
    assert response.status_code == 200
    assert "不认识的字段" in response.text and "可选的是" in response.text


def test_missing_codes_are_named_with_their_class(logged_screen):
    default_seed(logged_screen)
    response = logged_screen.client.get("/screen", params=_condition_params())
    assert "无数 1 只 · pr_now：亏损（PE(动) ≤ 0）" in response.text
    assert "600000" in response.text
