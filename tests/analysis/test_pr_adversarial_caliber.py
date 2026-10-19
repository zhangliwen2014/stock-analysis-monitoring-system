"""对抗性测试（M2-C 第一道闸）：`pr()` 的算式、预估支、以及 basis 能否跟着数出门。

覆盖设计文档 §3（口径）、§7（四种拒绝）、§2（跨 basis 不可排序）承诺过的话，
打的是「屏幕上那句话与算式不一致」这一类：算错了会被发现，说错了不会。

`test_should_*` 断言的是「我认为必须成立、当场不成立」的那类——**允许是红的**，
红的那一条就是 finding，不许把断言改软来让它变绿。其余用例刻画当前行为。
全部离线（内存库 + 注入时钟），不联网。
"""

from __future__ import annotations

import contextlib
import io
import json
import math
from datetime import UTC, date, datetime

import polars as pl
import pytest
from support import FrozenClock, StubAdapter, sample_bars

import sams.cli as cli
from sams.analysis.fundamentals import BASIS_PE, BASIS_REFUSED, pr
from sams.bootstrap import build_container
from sams.core.fundamentals import BASIS_PB_ESTIMATE, RATIO_BOUNDS
from sams.services.screen import ScreenService
from sams.storage.repos.financials import FinancialRepo, ValuationRepo
from sams.storage.repos.scan_results import ScanResultRepo
from sams.storage.repos.symbols import SymbolRepo

FY = date(2023, 12, 31)
FY_PUBLISHED = date(2024, 3, 20)
FRESH = date(2024, 7, 5)
AS_OF = date(2024, 7, 9)

FINANCIAL_SCHEMA = {
    "market": pl.Utf8,
    "code": pl.Utf8,
    "report_date": pl.Date,
    "published_date": pl.Date,
    "roe": pl.Float64,
    "source": pl.Utf8,
}
VALUATION_SCHEMA = {
    "market": pl.Utf8,
    "code": pl.Utf8,
    "date": pl.Date,
    "pe_ttm": pl.Float64,
    "pb": pl.Float64,
    "source": pl.Utf8,
}


# ---------- 1. 算式：手算可核的那几个数 ----------


def test_the_scale_origin_is_exact_not_merely_approximate():
    """设计文档 §1 的自检例：PE=20、ROE=20% ⇒ PR = 1.00，要求逐位相等。

    整条刻度的原点钉在这个数上；如果它是 0.9999999999999999，那「PR = 1」就敲不中。
    """
    assert pr(pe_ttm=20.0, roe=0.20).value == 1.0


def test_the_threshold_itself_lands_exactly_on_the_boundary():
    """PE=8、ROE=0.20 ⇒ 0.40 整，正好是用户给的那条门槛。"""
    assert pr(pe_ttm=8.0, roe=0.20).value == 0.4


@pytest.mark.parametrize("roe", [0.0, -0.0, 1e-323])
def test_a_denominator_that_is_zero_or_denormal_gives_a_usable_answer_or_a_refusal(roe):
    """不抛 ZeroDivisionError、不把 inf 当成一个「数」交回调用方。

    `roe=0` 走拒绝（§7 第 1 种）。而 1e-323 是一个**通过仓库量级闸的有限正数**
    （`_prepare` 只挡非有限，`RATIO_BOUNDS` 只挡 abs > 5），于是 `20 ÷ 1e-323 ÷ 100`
    溢出成 inf，`pr()` 说它 `ok`——选股的比较 `round(inf, 6) > 0.4` 恰好把它挡在篮子外，
    但 `analysis fundamentals` 会打印 `PR = inf` 并退出 0。
    """
    result = pr(pe_ttm=20.0, roe=roe)
    if result.ok:
        assert math.isfinite(result.value), f"PR 交回了 {result.value!r}，它不是一个可用的数"


def test_missing_pe_and_losing_pe_produce_different_reasons():
    """`pe=None`（源没给这一列）与 `pe=-12.5`（亏损）的下一步是两条命令，不能共用文案。"""
    lost = pr(pe_ttm=-12.4964, pb=2.89)
    absent = pr(pe_ttm=None, pb=2.89)
    assert lost.basis == absent.basis == BASIS_REFUSED
    assert "不为正" in lost.reason and "缺失" in absent.reason
    assert lost.reason != absent.reason


@pytest.mark.parametrize(
    "kwargs",
    [
        {"pe_ttm": -5.0, "pb": 2.0, "roe": -0.10},
        {"pe_ttm": 0.0, "pb": 2.0, "roe": -0.10, "roe_estimate": -0.15},
        {"pe_ttm": -5.0, "pb": 2.0, "roe_estimate": -0.15},
    ],
)
def test_negative_roe_cannot_turn_positive_through_the_double_division(kwargs):
    """两次负号相消是这条公式最静默的一处：`2 ÷ (-0.1) ÷ (-0.1) ÷ 100 = 2.0`（正的）。

    一个正的、能进 `PR ≤ 0.4` 篮子的数，由两个坏数据算出来。
    """
    assert 2.0 / -0.1 / -0.1 / 100.0 == 2.0  # 手算：公式本身确实给得出正数
    result = pr(**kwargs)
    assert result.value is None and result.basis == BASIS_REFUSED


def test_the_estimate_branch_is_the_documented_number_and_says_so():
    result = pr(pe_ttm=-12.5, pb=2.0, roe_estimate=0.20)
    assert result.value == pytest.approx(0.5)
    assert result.basis == BASIS_PB_ESTIMATE
    assert (result.estimated, result.roe_used) == (True, 0.20)


def test_a_positive_pe_silently_ignores_the_estimate_it_was_handed():
    """刻画现状：PE 为正时 `--roe-estimate` 整个被丢掉，屏幕上没有一句「你的预估没用上」。"""
    result = pr(pe_ttm=20.0, roe=0.20, roe_estimate=0.9)
    assert result.value == pytest.approx(1.0)
    assert result.estimated is False and result.roe_used == 0.20


# ---------- 2. 预估支的口径：命令行到底挡不挡得住百分数 ----------


def test_an_estimate_written_as_a_percentage_would_make_pr_ten_thousand_times_smaller():
    """预估 ROE 被**平方**地用（`PB ÷ ROE ÷ ROE ÷ 100`），口径错一次，PR 错一万倍。

    这正是 D66 在命令行与算式两侧都设闸的理由：一支平方，写错一次就是四个数量级。
    """
    honest = pr(pe_ttm=-12.5, pb=2.0, roe_estimate=0.15)
    percent = pr(pe_ttm=-12.5, pb=2.0, roe_estimate=15.0)
    assert honest.value == pytest.approx(0.888888888888889, rel=1e-12)
    assert percent.value is None, "百分数写法被拒收，而不是给一个小一万倍的数"
    assert pytest.approx(honest.value) == 2.0 / 15.0 / 15.0 / 100.0 * 10000.0


def test_the_cli_rejects_an_estimate_above_the_storage_ceiling(db, settings):
    """`--roe-estimate 15` 被 `ratio_arg` 拒收（§3 末尾实测的那句「参数有误」）。"""
    put(db, "601088", roe=0.20, pe=-12.5, pb=2.0)
    assert run_analysis(db, settings, ["--roe-estimate", "15"]) == 2


def test_an_estimate_written_as_a_percentage_is_refused_inside_the_former_dead_zone(
    db, settings
):
    """D66 之前 `ratio_arg` 的上限取了 `RATIO_BOUNDS["roe"] = 5.0`，于是 5 被当合法小数放行。

    `--roe-estimate 5`（也就是「5%」的百分数写法）会得到 PR≈0.0008 而不是 8.0——一万倍的错，
    方向永远是「极度便宜」。而这一支适用的票恰恰是周期底部与困境反转（真实 ROE 就在
    0%–5% 这一带），也就是说最需要预估的那批票，其百分数写法恰好全在闸门之内。
    """
    assert RATIO_BOUNDS["roe"] == 1.0
    put(db, "601088", roe=0.20, pe=-12.5, pb=2.0)
    assert run_analysis(db, settings, ["--roe-estimate", "5"]) == 2
    assert run_analysis(db, settings, ["--roe-estimate", "1"]) == 2, "含边界：1 = 100%"
    assert run_analysis(db, settings, ["--roe-estimate", "0.15"]) == 0


def test_the_pr_json_stays_parseable_by_a_strict_reader(db, settings):
    """`--json` 是机器出口（D57），而 PR 的值曾经没有量级检查。

    `--roe-estimate 1e-320` 通过全部参数校验（0 < x ≤ 5），然后在 `PB ÷ ROE ÷ ROE` 里
    溢出成 inf：stdout 上是 `Infinity`——JSON 规范里没有这个字面量，`jq` 与 `JSON.parse`
    直接拒收，而进程退出码是 0（屏幕那行打印 `PR = inf`）。
    """
    put(db, "601088", roe=0.20, pe=-12.5, pb=2.0)
    raw = run_analysis_capture(db, settings, ["--roe-estimate", "1e-320", "--json"], ok=False)
    assert "Infinity" not in raw, f"`--json` 出口不是严格 JSON：{raw.strip()}"
    assert "NaN" not in raw, f"`--json` 出口不是严格 JSON：{raw.strip()}"
    payload = json.loads(raw)
    assert payload["pr"]["value"] is None, "溢出的那一个必须走拒绝，而不是给一个 inf"
    assert payload["exit"] == 1


# ---------- 3. 跨 basis 的可比性 ----------


def make_screen(db) -> ScreenService:
    return ScreenService(
        FinancialRepo(db),
        ValuationRepo(db),
        SymbolRepo(db),
        ScanResultRepo(db),
        clock=FrozenClock(datetime(2024, 7, 9, 15, tzinfo=UTC)),
    )


def put(db, code: str, *, roe: float | None, pe: float | None, pb: float | None) -> None:
    FinancialRepo(db).upsert(
        pl.DataFrame(
            [{"market": "A", "code": code, "report_date": FY,
              "published_date": FY_PUBLISHED, "roe": roe, "source": "akshare"}],
            schema=FINANCIAL_SCHEMA,
        )
    )
    ValuationRepo(db).upsert(
        pl.DataFrame(
            [{"market": "A", "code": code, "date": FRESH,
              "pe_ttm": pe, "pb": pb, "source": "akshare"}],
            schema=VALUATION_SCHEMA,
        )
    )


def test_basis_and_estimated_reach_the_screen_the_json_and_the_archive(db):
    """每一只进篮子的票都必须带着「这一支是哪条公式」——存档出口（D59）。"""
    put(db, "601088", roe=0.20, pe=8.0, pb=2.0)
    make_screen(db).pr_max("A", 0.4, as_of=AS_OF)
    record = ScanResultRepo(db).get(on=AS_OF, market="A", strategy_id="pr<=0.4|annual")
    hit = record["hits"][0]
    assert hit["basis"] == BASIS_PE and hit["estimated"] is False
    assert hit["roe_used"] == 0.20
    assert hit["period"] == str(FY) and hit["valuation_on"] == str(FRESH), "两个粒度各说各的日"


def test_the_json_payload_names_the_branch_and_the_caliber(db, settings):
    put(db, "601088", roe=0.20, pe=20.0, pb=2.0)
    payload = run_analysis_json(db, settings, [])
    assert payload["pr"]["basis"] == BASIS_PE
    assert payload["roe"] == 0.20, "JSON 里给库里口径（小数），不给屏幕上的百分数"
    assert "小数" in payload["caliber"] and "分母口径" in payload["basis_note"]


def test_should_not_sort_pe_basis_and_estimate_basis_in_one_ascending_list(db):
    """§2：「跨 basis 的 PR 不可直接排序」——`--include-pb-branch` 给的正是这么一篮子。

    只按数值升序时，两条公式算出的数并排站，屏幕上除了一列 `pb_estimate` 字样没有
    任何一句「这两段不可比」。所以排序先按支分组、组内升序，PE 那一支在前。
    """
    put(db, "600001", roe=0.20, pe=12.0, pb=1.0)  # basis=pe，PR=0.6
    put(db, "600002", roe=0.40, pe=-9.0, pb=1.0)  # basis=pb_estimate，PR=0.0625
    scan = make_screen(db).pr_max("A", 0.7, as_of=AS_OF, include_pb_branch=True, record=False)
    bases = [hit.basis for hit in scan.hits]
    assert len(bases) == 2
    assert bases == [BASIS_PE, BASIS_PB_ESTIMATE], (
        f"两条公式的数被混进同一个升序篮子（{bases}），PB 那一支排在真数前面"
    )


def test_should_warn_on_screen_when_the_pb_branch_is_mixed_in(db, settings, capsys):
    """§2 承诺「并在表头写明」：混进第二支时，屏幕上必须有一句「这些数彼此不可比」。

    这里只铺一只票，且它走的是 PB 那一支，所以任何「可比」的话都只能来自这条承诺。
    """
    put(db, "600002", roe=0.40, pe=-9.0, pb=1.0)
    code = run_screen(db, settings, ["--include-pb-branch"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "可比" in out, (
        f"--include-pb-branch 的那一篮子没有带出「这些数彼此不可比」那句话：\n{out}"
    )


def test_should_not_call_a_disclosed_roe_an_estimate_when_the_pb_branch_is_included(db):
    """`screen pr` 压根没有 `--roe-estimate`（`pr_max` 不往 `pr()` 传它）。

    于是 PB 那一支用的永远是**已披露**那期的 ROE、`estimated=False`。开关名与排除那句
    都不许说「嵌了预估 ROE」——那是在替数据撒一个不存在的数。
    """
    put(db, "600002", roe=0.40, pe=-9.0, pb=1.0)
    excluded = make_screen(db).pr_max("A", 0.4, as_of=AS_OF, record=False).excluded
    included = make_screen(db).pr_max(
        "A", 0.4, as_of=AS_OF, include_pb_branch=True, record=False
    ).hits
    assert len(excluded) == 1 and len(included) == 1
    assert included[0].estimated is False, "这一支压根没用预估"
    assert "预估" not in excluded[0].reason, (
        f"排除原因说「嵌了预估 ROE」，同一只票进篮子时 estimated=False：{excluded[0]}"
    )


# ---------- 4. 门槛比较与存档身份 ----------


def test_the_threshold_tolerates_one_millionth_and_the_display_hides_it(db):
    """刻画现状：比较用 6 位小数，所以 PR=0.4000004 严格大于门槛仍算命中（§8.2 明说）。

    打印只有 3 位小数（`PR 0.400`），屏幕上看不出越界。1e-6 相对 PR 的量级（≈1）可以
    接受，这条用例的任务是把这个容忍带钉死，不让它被无意放宽。0.41 必须不入。
    """
    put(db, "600001", roe=0.20, pe=8.00001, pb=1.0)  # PR = 0.4000005
    put(db, "600002", roe=0.20, pe=8.02, pb=1.0)  # PR = 0.401
    scan = make_screen(db).pr_max("A", 0.4, as_of=AS_OF, record=False)
    assert [hit.code for hit in scan.hits] == ["600001"], "1e-6 带内算命中"
    assert scan.hits[0].value > 0.4, "命中的那只确实严格大于门槛"
    assert scan.priced_out == 1, "0.401 那只必须落选"


def test_a_subset_scan_leaves_the_days_full_basket_alone(db):
    """`strategy_id` 只含「门槛|口径|有没有混 PB 那一支」，刻意不含 `--codes`。

    于是当天先全量跑一次、再 `--codes 601088` 试一只票，第二次会**覆盖** `scan_results`
    里那一条（同 (date, market, strategy_id) 覆盖写），而 M3 的通知层读到的「今天这一篮子」
    变成一只票、`universe_size` 从 2 变成 1。屏幕上没有任何一处说这次覆盖发生过。
    契约（D57）：把 codes 编进 id 会让存档变成 N 行试跑，通知层不知道读哪一行——
    所以子集扫描不存档，全量那一条原样留着。
    """
    put(db, "601088", roe=0.20, pe=8.0, pb=1.0)
    put(db, "600001", roe=0.20, pe=8.0, pb=1.0)
    screen = make_screen(db)
    full = screen.pr_max("A", 0.4, as_of=AS_OF)
    assert len(full.hits) == 2
    subset = screen.pr_max("A", 0.4, as_of=AS_OF, codes=["601088"])
    assert subset.archived is False, "子集扫描写了存档"
    record = ScanResultRepo(db).get(on=AS_OF, market="A", strategy_id=full.strategy_id)
    assert len(record["hits"]) == 2, (
        f"一次 --codes 试跑把全量扫描的存档压成 {record['hits']}"
        f"（universe_size={record['universe_size']}）"
    )


def test_should_reject_a_threshold_that_rounds_down_to_zero(db, settings):
    """`_threshold` 只查 `> 0`，而比较用 `round(threshold, 6)`（cli/screen.py + screen.py:234）。

    `--pr-max 0.0000004` 合法通过校验，然后与 0.0 比——§7 明说「0 在 PR 的刻度上意思是
    免费，是这条公式最贵的一个误读」。门槛因此能被打磨成「只收免费的票」这句话。
    """
    put(db, "601088", roe=0.20, pe=8.0, pb=1.0)
    code = run_screen(db, settings, [], threshold="0.0000004")
    assert code == 2, f"门槛 round 成 0.0 仍被接受（退出码 {code}）"


# ---------- 5. 闸没挡住时，屏幕上那句话还在宣布小数口径 ----------


def test_a_denominator_the_guard_nulled_never_shares_the_screen_with_the_caliber_note(
    db, settings
):
    """D54 那句话本身：算式与声明同口径才叫诚实（D66 把它变成可核对的）。

    口径闸收在 1.0 之前，`roe=3.5` 能活着进库、活着进 PR，而同一行里 `caliber_note()`
    还在无条件宣布「比率列一律小数」——屏幕上出现「ROE 350.00%」与「PR 0.057」并列。
    """
    put(db, "601088", roe=3.5, pe=20.0, pb=2.0)  # 未 ÷100 的「3.5%」
    payload = run_analysis_json(db, settings, [], ok=False)
    assert payload["roe"] is None, "口径闸必须把那一个置空（D66）"
    assert payload["pr"]["value"] is None and payload["pr"]["basis"] == BASIS_REFUSED
    assert (payload["status"], payload["exit"]) == ("refused", 1)
    assert "小数" in payload["caliber"]


# ---------- CLI 脚手架 ----------


def container(db, settings):
    built = build_container(
        settings,
        db=db,
        clock=FrozenClock(datetime(2024, 7, 9, 15, tzinfo=UTC)),
        # 桩源只为过 registry 的路由校验：这些用例读的都是内存库里铺好的行，不联网
        adapters={
            name: StubAdapter(name, markets, sample_bars(source=name))
            for name, markets in (
                ("akshare", ["A"]),
                ("eastmoney", ["A"]),
                ("yfinance", ["HK", "US"]),
            )
        },
    )
    built.registry.sleep = lambda _seconds: None
    return built


def _dispatch(db, settings, argv: list[str]):
    args = cli.build_parser().parse_args(argv)
    return args, args.handler(container(db, settings), args)


def run_analysis(db, settings, argv: list[str]) -> int:
    _, code = _dispatch(
        db, settings, ["analysis", "fundamentals", "--market", "A", "--code", "601088", *argv]
    )
    return code


def run_analysis_capture(db, settings, argv: list[str], *, ok: bool = True) -> str:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        _, code = _dispatch(
            db, settings, ["analysis", "fundamentals", "--market", "A", "--code", "601088", *argv]
        )
    if ok:
        assert code == 0, f"命令没出数（退出码 {code}），读不到 payload"
    return buffer.getvalue()


def run_analysis_json(db, settings, argv: list[str], *, ok: bool = True) -> dict:
    return json.loads(run_analysis_capture(db, settings, [*argv, "--json"], ok=ok))


def run_screen(db, settings, argv: list[str], *, threshold: str = "0.4") -> int:
    _, code = _dispatch(
        db, settings, ["screen", "pr", "--market", "A", "--pr-max", threshold, *argv]
    )
    return code


