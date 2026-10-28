"""选股页：范围行、条件编辑、三个数、无数原因、加自选、方案（D180/D182/D184）。"""

from __future__ import annotations

from datetime import UTC, date, datetime

import polars as pl

from sams.core.screener_fields import Condition
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


def test_results_are_paginated_fifty_at_a_time_and_page_two_keeps_the_conditions(logged_screen):
    codes = [f"{600000 + i:06d}" for i in range(55)]
    seed(
        logged_screen.context,
        *(snap(code) for code in codes),
        codes=tuple(codes),
    )
    first = logged_screen.client.get("/screen", params=_condition_params())
    assert "第 1 / 2 页" in first.text
    assert codes[49] in first.text and codes[50] not in first.text
    second = logged_screen.client.get(
        "/screen", params=_condition_params() + [("page", "2")]
    )
    assert "第 2 / 2 页" in second.text
    assert codes[50] in second.text and codes[0] not in second.text


def test_a_page_number_past_the_end_lands_on_the_last_page(logged_screen):
    seed(logged_screen.context, snap("600519"))
    response = logged_screen.client.get(
        "/screen", params=_condition_params() + [("page", "99")]
    )
    assert "第 1 / 1 页" in response.text and "600519" in response.text


def _alice(logged_screen):
    return logged_screen.context.users.get_by_username("alice").id


def test_one_row_adds_through_the_watchlist_write_point(logged_screen):
    default_seed(logged_screen)
    response = logged_screen.client.post(
        "/screen/watchlist",
        data={
            "csrf": logged_screen.csrf,
            "market": "A",
            "f": "pr_now",
            "o": "<=",
            "v": "0.5",
            "action": "one:600519",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert logged_screen.context.watchlists.contains(_alice(logged_screen), "A", "600519")


def test_checked_codes_are_added_and_unticked_ones_are_not(logged_screen):
    default_seed(logged_screen)
    logged_screen.client.post(
        "/screen/watchlist",
        data={
            "csrf": logged_screen.csrf,
            "market": "A",
            "f": "pr_now",
            "o": "<=",
            "v": "0.5",
            "action": "checked",
            "codes": ["600519", "000001"],
        },
        follow_redirects=False,
    )
    context = logged_screen.context
    alice = _alice(logged_screen)
    assert context.watchlists.contains(alice, "A", "600519")
    assert context.watchlists.contains(alice, "A", "000001")
    assert not context.watchlists.contains(alice, "A", "300750")


def test_add_all_first_shows_a_dry_run_and_writes_nothing(logged_screen):
    default_seed(logged_screen)
    context = logged_screen.context
    alice = _alice(logged_screen)
    context.watchlists.add(alice, "A", "600519")
    preview = logged_screen.client.post(
        "/screen/watchlist",
        data={
            "csrf": logged_screen.csrf,
            "market": "A",
            "f": "pr_now",
            "o": "<=",
            "v": "0.5",
            "action": "all",
        },
        follow_redirects=False,
    )
    assert preview.status_code == 303 and "confirm=all" in preview.headers["location"]
    page = logged_screen.client.get(preview.headers["location"])
    assert "全部 2 只里有 1 只已在自选" in page.text
    assert "这次会加入 1 只" in page.text
    assert not context.watchlists.contains(alice, "A", "000001")


def test_add_all_confirmed_re_runs_the_conditions_on_the_server(logged_screen):
    default_seed(logged_screen)
    response = logged_screen.client.post(
        "/screen/watchlist",
        data={
            "csrf": logged_screen.csrf,
            "market": "A",
            "f": "pr_now",
            "o": "<=",
            "v": "0.5",
            "action": "all",
            "confirm": "yes",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    context = logged_screen.context
    alice = _alice(logged_screen)
    assert context.watchlists.contains(alice, "A", "600519")
    assert context.watchlists.contains(alice, "A", "000001")
    assert not context.watchlists.contains(alice, "A", "300750")


def _save(logged_screen, name="便宜", *, market="A", f="pr_now", o="<=", v="0.5"):
    return logged_screen.client.post(
        "/screen/presets",
        data={"csrf": logged_screen.csrf, "market": market, "f": f, "o": o, "v": v, "name": name},
        follow_redirects=False,
    )


def test_save_then_the_list_reads_back_as_a_sentence_not_json(logged_screen):
    default_seed(logged_screen)
    response = _save(logged_screen)
    assert response.status_code == 303
    page = logged_screen.client.get("/screen")
    assert "便宜" in page.text
    assert "现值市赚率 PR（小数比值（0.5 = 50%）） ≤ 0.5" in page.text


def test_saving_no_conditions_is_refused(logged_screen):
    response = logged_screen.client.post(
        "/screen/presets",
        data={"csrf": logged_screen.csrf, "market": "A", "f": "", "o": "<=", "v": "", "name": "空"},
    )
    assert response.status_code == 200 and "一条条件都没有" in response.text
    assert logged_screen.context.screener_presets.list(_alice(logged_screen)) == []


def test_a_duplicate_name_is_an_error_not_a_silent_overwrite(logged_screen):
    default_seed(logged_screen)
    _save(logged_screen)
    response = _save(logged_screen)
    assert response.status_code == 200
    assert "已有同名方案" in response.text
    assert len(logged_screen.context.screener_presets.list(_alice(logged_screen))) == 1


def test_run_links_back_to_the_preset_and_conditions_show_up_in_the_form(logged_screen):
    default_seed(logged_screen)
    _save(logged_screen)
    preset = logged_screen.context.screener_presets.list(_alice(logged_screen))[0]
    response = logged_screen.client.post(
        f"/screen/presets/{preset.id}/run",
        data={"csrf": logged_screen.csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303 and "preset=" in response.headers["location"]
    page = logged_screen.client.get(response.headers["location"])
    assert "通过 2 / 不通过 1 / 无数 1" in page.text


def test_rename_overwrite_and_delete(logged_screen):
    default_seed(logged_screen)
    _save(logged_screen)
    preset = logged_screen.context.screener_presets.list(_alice(logged_screen))[0]
    assert (
        logged_screen.client.post(
            f"/screen/presets/{preset.id}/rename",
            data={"csrf": logged_screen.csrf, "name": "很便宜"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    page = logged_screen.client.get("/screen")
    section = page.text.split("筛选方案")[1]
    # 新名字里也含「便宜」二字：按整格比对，才分得清旧行有没有留下
    assert "<td>很便宜</td>" in section and "<td>便宜</td>" not in section

    assert (
        logged_screen.client.post(
            f"/screen/presets/{preset.id}",
            data={
                "csrf": logged_screen.csrf,
                "market": "A",
                "f": "pr_now",
                "o": "<=",
                "v": "0.3",
            },
            follow_redirects=False,
        ).status_code
        == 303
    )
    page = logged_screen.client.get(f"/screen?preset={preset.id}")
    assert 'value="0.3"' in page.text

    assert (
        logged_screen.client.post(
            f"/screen/presets/{preset.id}/delete",
            data={"csrf": logged_screen.csrf},
            follow_redirects=False,
        ).status_code
        == 303
    )
    assert logged_screen.context.screener_presets.list(_alice(logged_screen)) == []


def test_another_users_preset_is_a_404_without_saying_whose_it_is(logged_screen):
    other = logged_screen.context.users.create("bob", "correct-horse-battery")
    foreign = logged_screen.context.screener_presets.create(
        other.id, "别人的", "A", [Condition("pr_now", "<=", 0.5)]
    )
    for path in (
        f"/screen/presets/{foreign.id}/run",
        f"/screen/presets/{foreign.id}/delete",
    ):
        response = logged_screen.client.post(
            path, data={"csrf": logged_screen.csrf}, follow_redirects=False
        )
        assert response.status_code == 404
    assert logged_screen.context.screener_presets.list(other.id) != []
