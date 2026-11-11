"""选股页：范围行、条件编辑、三个数、无数原因、加自选、方案（D180/D182/D184）。"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from urllib.parse import parse_qs, unquote, urlsplit

import polars as pl

from sams.core.screener_fields import Condition
from sams.datasource.spot import SPOT_SCHEMA
from sams.storage.repos.financials import FinancialRepo
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


def seed_hk(context, *rows, codes=None):
    codes = codes or [row["code"] for row in rows]
    SymbolListRepo(context.db).upsert_batch(
        "HK",
        [
            {"code": code, "name": code, "py_initials": code, "py_full": code, "currency": "HKD"}
            for code in codes
        ],
    )
    frame = pl.DataFrame(
        [{"market": "HK", "taken_at": TAKEN, **row} for row in rows],
        schema={
            "market": pl.Utf8,
            "taken_at": pl.Datetime("us", "UTC"),
            **SPOT_SCHEMA,
        },
    )
    SnapshotRepo(context.db).replace_day(market="HK", day=DAY, taken_at=TAKEN, frame=frame)


def seed_us(context, *rows, codes=None):
    codes = codes or [row["code"] for row in rows]
    SymbolListRepo(context.db).upsert_batch(
        "US",
        [
            {"code": code, "name": code, "py_initials": code, "py_full": code, "currency": "USD"}
            for code in codes
        ],
    )
    frame = pl.DataFrame(
        [{"market": "US", "taken_at": TAKEN, **row} for row in rows],
        schema={
            "market": pl.Utf8,
            "taken_at": pl.Datetime("us", "UTC"),
            **SPOT_SCHEMA,
        },
    )
    SnapshotRepo(context.db).replace_day(market="US", day=DAY, taken_at=TAKEN, frame=frame)


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
    assert "快照 2026-10-28 · 票池 4 只" in response.text
    assert "加一条条件" in response.text


def test_the_range_line_says_which_columns_belong_to_that_day(logged_screen):
    """快照日管的是除价格外的每一列，这句话不能省（D288）。

    价格列有自己的时点（盘中/收盘逐行在标），「快照 10-28」顶在表头旁边很容易被
    读成整张表都是 10-28 的数——那样盘中价就成了快照日收盘价。
    """
    default_seed(logged_screen)
    response = logged_screen.client.get("/screen", params={"market": "A"})
    assert "除「价格」外的列都是这一天的数" in response.text


def test_the_preset_card_is_in_the_page_body_not_the_head(logged_screen):
    """方案卡曾整段落在 `{% block title %}` 里：浏览器把标题后的一切当文本，正文里就没有它。"""
    response = logged_screen.client.get("/screen", params={"market": "A"})
    head, _, body = response.text.partition("<body>")
    assert "筛选方案" not in head
    assert "筛选方案" in body


def test_a_condition_in_the_second_row_alone_still_counts_and_can_be_saved(logged_screen):
    default_seed(logged_screen)
    params = [
        ("market", "A"),
        ("f", ""),
        ("o", "<="),
        ("v", ""),
        ("f", "pr_now"),
        ("o", "<="),
        ("v", "0.5"),
    ]
    response = logged_screen.client.get("/screen", params=params)
    assert "通过 2 / 不通过 1 / 无数 1" in response.text
    assert "保存当前条件" in response.text
    assert "先在上面填一条条件" not in response.text


def test_the_result_line_says_which_way_it_was_ranked(logged_screen):
    default_seed(logged_screen)
    params = [("market", "A"), ("f", "pr_now"), ("o", "<="), ("v", "0.5")]
    response = logged_screen.client.get("/screen", params=params)
    assert "按现值市赚率 PR 从低到高排序" in response.text


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


def test_the_empty_snapshot_card_points_at_its_own_recollect_button(logged_screen):
    """R3-02/T1a：「立即重采」就在同一张卡里，不该叫网页用户去跑命令行。"""
    response = logged_screen.client.get("/screen", params={"market": "HK"})
    assert "立即重采" in response.text
    assert "sams sync spot" not in response.text


def test_a_market_whose_columns_do_not_exist_says_why_the_fields_are_greyed(logged_screen):
    """灰着的字段本该在屏幕上自证：`disabled title` 只在鼠标悬停时说话，键盘与触屏看不到。"""
    response = logged_screen.client.get("/screen", params={"market": "HK"})
    assert "灰着不是算出来 0" in response.text
    assert "个字段" in response.text


def test_technical_fields_are_only_offered_for_a_shares(logged_screen):
    """技术面 v0 只开 A 股（D251）：港股下拉里灰着，A 股下拉里可选。

    「灰着」不是 0：它说的是这只票/这个市场没有一条能过复权闸的日线，而不是指标等于零。
    """
    a_page = logged_screen.client.get("/screen", params={"market": "A"})
    assert '<option value="ma20_gap" disabled' not in a_page.text
    hk_page = logged_screen.client.get("/screen", params={"market": "HK"})
    assert '<option value="ma20_gap" disabled' in hk_page.text


def test_the_us_page_screens_on_the_columns_its_snapshot_actually_has(logged_screen):
    """美股快照只验过 7 列（D226）：价格能筛，PB/市赚率灰着并说清为什么。"""
    seed_us(
        logged_screen.context,
        {**snap("AAPL", pe_dyn=40.0, pb=None), "price": 348.91},
        {**snap("F", pe_dyn=8.0, pb=None), "price": 9.5},
    )
    page = logged_screen.client.get(
        "/screen",
        params=[("market", "US"), ("f", "price"), ("o", "<="), ("v", "10")],
    )
    assert page.status_code == 200
    assert "快照 2026-10-28 · 票池 2 只" in page.text
    assert "通过 1 / 不通过 1 / 无数 0" in page.text
    assert '<option value="price" disabled' not in page.text
    assert '<option value="pe_ttm" disabled' not in page.text
    assert '<option value="pb" disabled' in page.text
    assert '<option value="pr_now" disabled' in page.text


def test_the_annual_pr_condition_composes_with_another_condition(logged_screen):
    """D335：网页能写「年报口径 PR ≤ 0.5 且资产负债率 ≤ 0.6」，结果行带两列的值。"""
    seed(
        logged_screen.context,
        snap("600519", pe_dyn=20.0),
        snap("300750", pe_dyn=5.0),
    )
    FinancialRepo(logged_screen.context.db).upsert(
        pl.DataFrame(
            [
                {
                    "market": "A",
                    "code": code,
                    "report_date": date(2025, 12, 31),
                    "published_date": date(2026, 3, 20),
                    "roe": roe,
                    "debt_ratio": debt,
                    "source": "akshare",
                }
                for code, roe, debt in (
                    ("600519", 0.20, 0.30),
                    ("300750", 0.25, 0.40),
                )
            ]
        )
    )
    response = logged_screen.client.get(
        "/screen",
        params=[
            ("market", "A"),
            ("f", "pr_annual"),
            ("o", "<="),
            ("v", "0.5"),
            ("f", "debt_ratio"),
            ("o", "<="),
            ("v", "0.6"),
        ],
    )
    assert response.status_code == 200
    assert "通过 1 / 不通过 1 / 无数 0" in response.text
    assert "按年报市赚率 PR 从低到高排序" in response.text
    assert "/symbol/A/300750" in response.text
    assert "/symbol/A/600519" not in response.text


def test_the_annual_pr_is_offered_in_all_three_markets(logged_screen):
    """PE(TTM) 与年报 ROE 三个市场都有；它不该像 pr_now 那样在 HK/US 灰着。"""
    for market in ("A", "HK", "US"):
        page = logged_screen.client.get("/screen", params={"market": market})
        assert '<option value="pr_annual"' in page.text
        assert '<option value="pr_annual" disabled' not in page.text


def _annual_rows(context, code="600519", roe=0.20):
    FinancialRepo(context.db).upsert(
        pl.DataFrame(
            [
                {
                    "market": "A",
                    "code": code,
                    "report_date": date(2025, 12, 31),
                    "published_date": date(2026, 3, 20),
                    "roe": roe,
                    "source": "akshare",
                }
            ]
        )
    )


def _roe_params():
    return [("market", "A"), ("f", "roe"), ("o", ">="), ("v", "0.1")]


def test_the_missing_annual_group_offers_a_queue_button_not_a_command(logged_screen):
    """R3-02/D336：网页用户没有终端；缺年报的下一步是排进同一条补采队列。"""
    seed(logged_screen.context, snap("600519"), snap("000001"))
    _annual_rows(logged_screen.context)
    page = logged_screen.client.get("/screen", params=_roe_params())
    assert "没有已披露年报数据（年报 ROE）" in page.text
    assert "sams sync financials" not in page.text
    assert "把这 1 只加入补采队列" in page.text


def test_the_queue_button_enqueues_the_group_and_the_second_press_says_it_is_queued(
    logged_screen,
):
    seed(logged_screen.context, snap("600519"), snap("000001"))
    _annual_rows(logged_screen.context)
    form = {
        "csrf": logged_screen.csrf,
        "market": "A",
        "field": "roe",
        "kind": "financials",
        "f": ["roe"],
        "o": [">="],
        "v": ["0.1"],
    }
    response = logged_screen.client.post(
        "/screen/backfill", data=form, follow_redirects=False
    )
    assert response.status_code == 303
    ctx = logged_screen.context
    row = ctx.backfills_store.open_for("A", "000001", kind="financials")
    assert row is not None and row.status == "pending"
    page = logged_screen.client.get(response.headers["location"])
    assert "新排进补采队列 1 只" in page.text
    assert "后台逐只同步年报，补完刷新这一页" in page.text

    again = logged_screen.client.post(
        "/screen/backfill", data=form, follow_redirects=False
    )
    page = logged_screen.client.get(again.headers["location"])
    assert "1 只本来就在队列里" in page.text


def test_a_group_that_cannot_be_backfilled_is_not_queued(logged_screen):
    """页面过期（这一组已经不在了）时不许假装排过：notice 说清，队列里也不多行。"""
    seed(logged_screen.context, snap("600519"))
    response = logged_screen.client.post(
        "/screen/backfill",
        data={
            "csrf": logged_screen.csrf,
            "market": "A",
            "field": "pr_now",
            "kind": "financials",
            "f": ["roe"],
            "o": [">="],
            "v": ["0.1"],
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = logged_screen.client.get(response.headers["location"])
    assert "现在不在可补采的范围里" in page.text
    assert logged_screen.context.backfills_store.open_for(
        "A", "600519", kind="financials"
    ) is None


_TECH_PARAMS = [("market", "A"), ("f", "ma20_gap"), ("o", ">="), ("v", "0")]


def _seed_bars(context, code="600519", n=260):
    from sams.storage.repos.bars import BarRepo

    days = []
    cursor = DAY
    while len(days) < n:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    days.reverse()
    closes = [100.0 + i * 0.5 for i in range(n)]
    BarRepo(context.db).upsert(
        pl.DataFrame(
            {
                "market": ["A"] * n,
                "code": [code] * n,
                "date": days,
                "open": closes,
                "high": [value + 1 for value in closes],
                "low": [value - 1 for value in closes],
                "close": closes,
                "volume": [100.0] * n,
                "amount": [value * 100 for value in closes],
                "source": ["akshare"] * n,
            }
        )
    )


def test_a_bars_gap_offers_the_queue_button_not_a_terminal(logged_screen):
    """D337：日线不足的下一步是排队，不是让网页用户去敲 `sams sync bars`。"""
    seed(logged_screen.context, snap("600519"))
    page = logged_screen.client.get("/screen", params=_TECH_PARAMS)
    assert "日线不足 20 根" in page.text
    assert "sams sync bars" not in page.text
    assert "把这 1 只加入补采队列" in page.text
    assert "后台逐只补日线，补完刷新这一页" in page.text


def test_the_bars_button_enqueues_bars_and_the_second_press_says_it_is_queued(
    logged_screen,
):
    seed(logged_screen.context, snap("600519"))
    form = {
        "csrf": logged_screen.csrf,
        "market": "A",
        "field": "ma20_gap",
        "kind": "bars",
        "f": ["ma20_gap"],
        "o": [">="],
        "v": ["0"],
    }
    response = logged_screen.client.post(
        "/screen/backfill", data=form, follow_redirects=False
    )
    assert response.status_code == 303
    ctx = logged_screen.context
    row = ctx.backfills_store.open_for("A", "600519", kind="bars")
    assert row is not None and row.status == "pending"
    page = logged_screen.client.get(response.headers["location"])
    assert "新排进补采队列 1 只" in page.text
    assert "后台逐只补日线，补完刷新这一页" in page.text

    again = logged_screen.client.post(
        "/screen/backfill", data=form, follow_redirects=False
    )
    page = logged_screen.client.get(again.headers["location"])
    assert "1 只本来就在队列里" in page.text


def test_a_coverage_gap_enqueues_actions_not_bars(logged_screen):
    seed(logged_screen.context, snap("600519"))
    _seed_bars(logged_screen.context)
    page = logged_screen.client.get("/screen", params=_TECH_PARAMS)
    assert "公司行为覆盖未确认，无法复权" in page.text
    assert "后台逐只同步公司行为，补完刷新这一页" in page.text
    response = logged_screen.client.post(
        "/screen/backfill",
        data={
            "csrf": logged_screen.csrf,
            "market": "A",
            "field": "ma20_gap",
            "kind": "actions",
            "f": ["ma20_gap"],
            "o": [">="],
            "v": ["0"],
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    ctx = logged_screen.context
    assert ctx.backfills_store.open_for("A", "600519", kind="actions") is not None
    assert ctx.backfills_store.open_for("A", "600519", kind="bars") is None


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


def test_the_range_line_survives_a_successful_screen(logged_screen):
    """范围与结果出自同一次采：跑出三个数之后不能反过来宣称「还没有快照」。"""
    default_seed(logged_screen)
    response = logged_screen.client.get("/screen", params=_condition_params())
    assert response.status_code == 200
    assert "快照 2026-10-28 · 票池 4 只" in response.text
    assert "采集" in response.text
    assert "还没有全市场快照" not in response.text


def test_every_condition_reports_passed_failed_missing_and_the_hits(logged_screen):
    default_seed(logged_screen)
    response = logged_screen.client.get("/screen", params=_condition_params())
    assert response.status_code == 200
    assert "通过 2 / 不通过 1 / 无数 1" in response.text
    assert "600519" in response.text and "000001" in response.text
    assert "300750" not in response.text


def test_each_hit_links_to_its_symbol_page(logged_screen):
    """结果表不是死文字：点代码或名字就进这只票的现场（详情、K 线、规则、论）。

    筛出「PR ≤ 0.5」之后，下一步一定是「它长什么样」；让人再去另一个页面重搜一遍，
    正是「两眼一抹黑」的那一步（D211）。
    """
    default_seed(logged_screen)
    response = logged_screen.client.get("/screen", params=_condition_params())
    assert response.status_code == 200
    assert 'href="/symbol/A/600519?back=' in response.text
    assert 'href="/symbol/A/000001?back=' in response.text


def test_the_hit_link_carries_the_way_back_to_the_same_result_page(logged_screen):
    """进详情再回来，那一屏还是原来那一屏：条件、页号都跟着去、跟着回。

    只把 `/screen` 当返回目标是另一种麻烦——人得把刚填的条件再填一遍，
    「看一眼细节」就成了重做一遍选股的代价。
    """
    default_seed(logged_screen)
    params = _condition_params() + [("f", "turnover_rate"), ("o", ">="), ("v", "1")]
    response = logged_screen.client.get("/screen", params=params)
    match = re.search(r'href="/symbol/A/000001\?back=([^"]+)"', response.text)
    assert match is not None
    back = parse_qs(urlsplit(unquote(match.group(1))).query)
    assert back["market"] == ["A"]
    assert back["f"] == ["pr_now", "turnover_rate"]
    assert back["o"] == ["<=", ">="]
    assert back["v"] == ["0.5", "1"]


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


def test_a_bad_valuation_value_names_the_field_in_chinese(logged_screen):
    """R-09：`v=abc` 的错误要叫人看得懂——屏幕上是「市盈率(TTM)」，不是 `pe_ttm`。"""
    response = logged_screen.client.get(
        "/screen",
        params=[("market", "A"), ("f", "pe_ttm"), ("o", "<="), ("v", "abc")],
    )
    assert response.status_code == 200
    assert "市盈率(TTM) 的值要一个数" in response.text
    assert "pe_ttm 的值" not in response.text


def test_negative_valuations_are_left_out_and_the_count_is_stated(logged_screen):
    """R-09：`PE(TTM) ≤ 8` 的首屏不能把 -72980.5 当成最便宜的那只。"""
    seed(
        logged_screen.context,
        snap("600519", pe_dyn=5.0),
        snap("000001", pe_dyn=-72980.5),
    )
    page = logged_screen.client.get(
        "/screen",
        params=[("market", "A"), ("f", "pe_ttm"), ("o", "<="), ("v", "8")],
    )
    assert page.status_code == 200
    assert "600519" in page.text
    assert "票000001" not in page.text
    assert "估值不为正" in page.text
    assert "其中 1 只" in page.text
    assert 'name="include_negatives"' in page.text


def test_the_nonpositive_group_does_not_hand_the_web_user_a_command_line(logged_screen):
    """R3-02/T1a：同一句原因也上网页。网页用户的下一步是上面那颗勾选框，
    不是 `--include-negatives`——那句话要说给命令行听（D283：CLI 自己附加）。"""
    seed(
        logged_screen.context,
        snap("600519", pe_dyn=5.0),
        snap("000001", pe_dyn=-72980.5),
    )
    page = logged_screen.client.get(
        "/screen",
        params=[("market", "A"), ("f", "pe_ttm"), ("o", "<="), ("v", "8")],
    )
    assert "估值不为正" in page.text
    assert "命令行" not in page.text
    assert "--include-negatives" not in page.text


def test_the_include_negatives_toggle_brings_the_row_back(logged_screen):
    seed(
        logged_screen.context,
        snap("600519", pe_dyn=5.0),
        snap("000001", pe_dyn=-72980.5),
    )
    page = logged_screen.client.get(
        "/screen",
        params=[
            ("market", "A"),
            ("f", "pe_ttm"),
            ("o", "<="),
            ("v", "8"),
            ("include_negatives", "1"),
        ],
    )
    assert "票000001" in page.text
    assert "估值不为正" not in page.text


def test_st_and_delisting_rows_are_hidden_by_default_and_counted(logged_screen):
    seed(
        logged_screen.context,
        snap("600519"),
        {**snap("600001"), "name": "*ST海医", "is_st": True},
        {**snap("600002"), "name": "退市海医", "is_st": False},
    )
    params = [("market", "A"), ("f", "price"), ("o", "<="), ("v", "20")]
    page = logged_screen.client.get("/screen", params=params)
    assert "600519" in page.text
    assert "*ST海医" not in page.text and "退市海医" not in page.text
    assert "ST/退市 2 只未参与" in page.text
    assert 'name="include_st"' in page.text

    shown = logged_screen.client.get("/screen", params=params + [("include_st", "1")])
    assert "*ST海医" in shown.text and "退市海医" in shown.text
    assert "ST/退市 2 只未参与" not in shown.text


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
    match = re.search(rf'href="/symbol/A/{codes[50]}\?back=([^"]+)"', second.text)
    assert match is not None
    assert "page=2" in unquote(match.group(1))


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


def test_checked_add_returns_to_the_same_page_with_the_ticks(logged_screen):
    """第 2 页勾选加自选后回到第 2 页，勾着的仍是原来那几只（R-13）。"""
    default_seed(logged_screen)
    response = logged_screen.client.post(
        "/screen/watchlist",
        data={
            "csrf": logged_screen.csrf,
            "market": "A",
            "f": "pr_now",
            "o": "<=",
            "v": "0.5",
            "action": "checked",
            "codes": ["600519"],
            "page": "2",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert "page=2" in location and "sel=600519" in location
    page = logged_screen.client.get(location)
    assert page.status_code == 200
    assert re.search(r'name="codes" value="600519" checked', page.text) is not None


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


def _save(
    logged_screen,
    name="便宜",
    *,
    market="A",
    f="pr_now",
    o="<=",
    v="0.5",
    description="",
):
    return logged_screen.client.post(
        "/screen/presets",
        data={
            "csrf": logged_screen.csrf,
            "market": market,
            "f": f,
            "o": o,
            "v": v,
            "name": name,
            "description": description,
        },
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


def test_edit_overwrite_and_delete(logged_screen):
    default_seed(logged_screen)
    _save(logged_screen, description="旧说明")
    preset = logged_screen.context.screener_presets.list(_alice(logged_screen))[0]
    assert (
        logged_screen.client.post(
            f"/screen/presets/{preset.id}/edit",
            data={
                "csrf": logged_screen.csrf,
                "name": "很便宜",
                "description": "只做底仓",
            },
            follow_redirects=False,
        ).status_code
        == 303
    )
    page = logged_screen.client.get("/screen")
    section = page.text.split("筛选方案")[1]
    # 新名字里也含「便宜」二字：按整格比对，才分得清旧行有没有留下。
    # 名字现在是一个链接（点它回到那一屏，spec 2026-11-03-back-link §3.3），
    # 所以「整格」是 `>` 到 `</a>` 那一段
    assert ">很便宜</a>" in section and ">便宜</a>" not in section
    assert "只做底仓" in section and "旧说明" not in section

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
    assert "只做底仓" in page.text

    assert (
        logged_screen.client.post(
            f"/screen/presets/{preset.id}/delete",
            data={"csrf": logged_screen.csrf},
            follow_redirects=False,
        ).status_code
        == 303
    )
    assert logged_screen.context.screener_presets.list(_alice(logged_screen)) == []


def test_a_description_longer_than_the_limit_is_refused_and_nothing_is_saved(logged_screen):
    response = logged_screen.client.post(
        "/screen/presets",
        data={
            "csrf": logged_screen.csrf,
            "market": "A",
            "f": "pr_now",
            "o": "<=",
            "v": "0.5",
            "name": "长说明",
            "description": "说" * 201,
        },
    )
    assert response.status_code == 200 and "说明最多" in response.text
    assert logged_screen.context.screener_presets.list(_alice(logged_screen)) == []


def test_a_preset_has_a_load_link_that_puts_its_conditions_back_in_the_form(logged_screen):
    default_seed(logged_screen)
    _save(logged_screen)
    preset = logged_screen.context.screener_presets.list(_alice(logged_screen))[0]
    page = logged_screen.client.get("/screen")
    assert f"/screen?market=A&amp;preset={preset.id}" in page.text
    loaded = logged_screen.client.get(f"/screen?preset={preset.id}")
    assert '<option value="pr_now" selected>' in loaded.text


def test_the_recommended_recipes_show_their_conditions_basis_and_caveat(logged_screen):
    page = logged_screen.client.get("/screen", params={"market": "A"})
    assert "推荐选股组合" in page.text
    for name in ("巴菲特：高 ROE 少负债", "格雷厄姆：安全边际", "林奇：成长与价格匹配"):
        assert name in page.text
    assert "依据：" in page.text and "注意：" in page.text
    # 配方带的是普通 GET 链接：载入后条件回到表单里，仍可继续改
    assert "载入并筛选" in page.text and "f=gross_margin" in page.text


def test_a_recipe_saved_as_my_preset_keeps_its_description(logged_screen):
    default_seed(logged_screen)
    response = _save(
        logged_screen,
        name="巴菲特：高 ROE 少负债",
        f="roe",
        o=">=",
        v="0.15",
        description="年报 ROE ≥ 15%、毛利率 ≥ 35%、资产负债率 ≤ 55%",
    )
    assert response.status_code == 303
    (preset,) = logged_screen.context.screener_presets.list(_alice(logged_screen))
    assert preset.name == "巴菲特：高 ROE 少负债"
    assert "毛利率" in preset.description


def test_a_market_without_recipes_does_not_show_the_recipe_card(logged_screen):
    page = logged_screen.client.get("/screen", params={"market": "HK"})
    assert "推荐选股组合" not in page.text


def test_the_hk_page_says_the_annual_is_taken_by_report_date_and_a_page_does_not(
    logged_screen,
):
    """港股年报没有披露日（D215）：屏幕必须说清它按报告期取；A 股仍按已披露日挡。"""
    seed_hk(logged_screen.context, snap("00700", pe_dyn=12.8))
    logged_screen.context.financials_store.upsert(
        pl.DataFrame(
            [
                {
                    "market": "HK",
                    "code": "00700",
                    "report_date": date(2025, 12, 31),
                    "published_date": None,
                    "profit_yoy": 0.1585,
                    "source": "eastmoney_hk_f10",
                }
            ],
            schema_overrides={"published_date": pl.Date},
        )
    )
    page = logged_screen.client.get(
        "/screen",
        params=[("market", "HK"), ("f", "peg"), ("o", "<="), ("v", "1")],
    )
    assert page.status_code == 200
    assert "报告期 ≤ 快照日" in page.text
    assert "通过 1 / 不通过 0 / 无数 0" in page.text
    assert "00700" in page.text

    default_seed(logged_screen)
    a_page = logged_screen.client.get(
        "/screen",
        params=[("market", "A"), ("f", "peg"), ("o", "<="), ("v", "1")],
    )
    assert "报告期 ≤ 快照日" not in a_page.text


def test_the_hk_page_reports_annual_coverage_so_a_short_list_is_not_misread(
    logged_screen,
):
    """「PEG ≤ 2 命中 1 只」旁边没有覆盖数，就会被读成「港股没有便宜票」（D227）。"""
    seed_hk(
        logged_screen.context,
        snap("00700", pe_dyn=12.8),
        snap("01021", pe_dyn=8.0),
    )
    logged_screen.context.financials_store.upsert(
        pl.DataFrame(
            [
                {
                    "market": "HK",
                    "code": "00700",
                    "report_date": date(2025, 12, 31),
                    "published_date": None,
                    "profit_yoy": 0.1585,
                    "source": "eastmoney_hk_f10",
                }
            ],
            schema_overrides={"published_date": pl.Date},
        )
    )
    page = logged_screen.client.get(
        "/screen",
        params=[("market", "HK"), ("f", "peg"), ("o", "<="), ("v", "1")],
    )
    assert "年报覆盖 1 / 2 只" in page.text
    assert "不是落选" in page.text

    pe_page = logged_screen.client.get(
        "/screen",
        params=[("market", "HK"), ("f", "pe_ttm"), ("o", "<="), ("v", "30")],
    )
    assert "年报覆盖" not in pe_page.text


def test_another_users_preset_is_a_404_without_saying_whose_it_is(logged_screen):
    other = logged_screen.context.users.create("bob", "correct-horse-battery")
    foreign = logged_screen.context.screener_presets.create(
        other.id, "别人的", "A", [Condition("pr_now", "<=", 0.5)]
    )
    for path in (
        f"/screen/presets/{foreign.id}/run",
        f"/screen/presets/{foreign.id}/edit",
        f"/screen/presets/{foreign.id}/delete",
    ):
        response = logged_screen.client.post(
            path,
            data={"csrf": logged_screen.csrf, "name": "抢走", "description": "x"},
            follow_redirects=False,
        )
        assert response.status_code == 404
    assert logged_screen.context.screener_presets.list(other.id) != []


def test_running_a_preset_records_the_basket_for_that_day(logged_screen):
    from sams.services.screener import preset_strategy_id

    default_seed(logged_screen)
    _save(logged_screen)
    preset = logged_screen.context.screener_presets.list(_alice(logged_screen))[0]
    logged_screen.client.post(
        f"/screen/presets/{preset.id}/run",
        data={"csrf": logged_screen.csrf},
        follow_redirects=False,
    )
    strategy = preset_strategy_id(preset.id, preset.conditions)
    records = logged_screen.context.scan_store.recent(market="A", strategy_id=strategy, limit=1)
    assert records and records[0]["date"] == DAY
    assert {hit["code"] for hit in records[0]["hits"]} == {"600519", "000001"}


def test_the_list_says_when_the_current_version_last_ran(logged_screen):
    default_seed(logged_screen)
    _save(logged_screen)
    preset = logged_screen.context.screener_presets.list(_alice(logged_screen))[0]
    page = logged_screen.client.get("/screen")
    assert "没跑过" in page.text
    logged_screen.client.post(
        f"/screen/presets/{preset.id}/run",
        data={"csrf": logged_screen.csrf},
        follow_redirects=False,
    )
    page = logged_screen.client.get("/screen")
    assert "没跑过" not in page.text
