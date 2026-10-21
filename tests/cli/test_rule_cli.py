"""`sams rule` 的行为契约：一屏给人、`--json` 给机器、拒绝也给（§10、D57/D77）。

用真仓库而不是替身：这一枚要验的三件事——`--param` 怎么落库、退出码按不按那条形、
改派生规则时论上那一列跟不跟着动——全都发生在真实的那几张表上。把仓库 stub 掉，
验的就只剩「命令行调了哪个方法」。

库里那个世界（`support_valuation`）：现价 17.0（`PRICE`）、合成分 68.96，
`TODAY` 是 2024-10-26（周六）。默认那条 `stop.fixed price=100000` 一定命中（现价远在
止损线下方，`stop_stage` 判成 hit）——注意不能拿 `price.cross_below` 当「一定命中」：
它的「下穿」要求前收盘还在线上，而这条序列天天在 17 附近，100000 那条线永远没被穿过。
`--target-ref A/000001` 则一定读不到（全库没有那根 K 线，主档也没有这个代码）。
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest
from support_valuation import CODE, make_container, seed_calendar

import sams.cli as cli
from sams.core.errors import ValidationError

USER = "trader"
PWD = "correct-horse-battery"
BAD_CODE = "000001"
T0 = datetime(2024, 10, 26, 15, 0, tzinfo=UTC)
LEVELS = {
    "entry_low": 30.0,
    "entry_high": 36.0,
    "stop_price": 28.0,
    "target_price": 52.0,
    "reward_risk": 2.0,
}


@pytest.fixture
def container(db, settings):
    c = make_container(db, settings)
    c.users.create(USER, PWD)
    seed_calendar(c)
    return c


def invoke(container, *extra: str) -> int:
    args = cli.build_parser().parse_args(["rule", *extra])
    return args.handler(container, args)


def out(container, capsys, *extra: str) -> tuple[int, str]:
    capsys.readouterr()
    code = invoke(container, *extra)
    return code, capsys.readouterr().out


def payload(container, capsys, *extra: str) -> tuple[int, dict]:
    code, text = out(container, capsys, *extra, "--json")
    return code, json.loads(text)


def uid(container) -> str:
    return container.users.get_by_username(USER).id


def open_account(container) -> str:
    return container.sim_accounts.open(uid(container), "SIM-1").id


def add_rule(container, capsys, *extra: str, code: str = CODE) -> dict:
    """默认建一条「一定命中」的规则：用例只改自己要改的那一两个键。"""
    got = payload(
        container,
        capsys,
        "add",
        "--user",
        USER,
        "--kind",
        "stop.fixed",
        "--name",
        "跌破十万",
        "--target-type",
        "symbol",
        "--target-ref",
        f"A/{code}",
        "--param",
        "price=100000",
        *extra,
    )
    assert got[0] == 0, got[1]
    return got[1]["rule"]


def make_thesis(container, *, style="value", **over):
    """论按**用户 id** 存（`ThesisService` 内部就是这么解析的），所以这里不能写字面量 `USER`：
    命令层 `--user trader` 解析出的是 id，写 "trader" 的论它一条也读不到。

    `style` 是给「同一个代码要同时挂两条活论」那一种用例留的口子：仓库对
    `(市场, 代码, style)` 只留一条活的（`_live_on_the_same_key`），换 style 才合法。
    """
    levels = dict(LEVELS)
    levels.update(over)
    return container.theses_store.create(
        user_id=uid(container),
        market="A",
        code=CODE,
        style=style,
        actor_id=uid(container),
        now=T0,
        status="proposed",
        **levels,
    )


def open_thesis(container, *, style="value"):
    """推进到 `opened`：复检只认 opened/tracking 那两档（§9.3）。"""
    thesis = make_thesis(container, style=style)
    owner = uid(container)
    container.theses_store.advance(owner, thesis.id, "opened", actor_id=owner, now=T0)
    return container.theses_store.get(owner, thesis.id)


def derive(container, capsys, thesis_id: str, *, account: str | None = None) -> dict:
    extra = ("--account", account) if account else ()
    code, row = payload(
        container, capsys, "derive", thesis_id, "--user", USER, *extra
    )
    assert code == 0, row
    return row


def kinds_of(container, capsys) -> dict:
    """按 kind 取名下那条规则的 payload（屏幕上那行 id 是断言的锚）。"""
    _, row = payload(container, capsys, "list", "--user", USER)
    return {item["kind"]: item for item in row["rules"]}


# ---------- kinds ----------


def test_kinds_names_every_template_and_its_required_params(container, capsys):
    """`--param` 的键名得有出口：不然用户唯一能试错的地方就是仓库那句「未知参数」。"""
    code, row = payload(container, capsys, "kinds")
    assert code == 0
    assert {"stop.fixed", "tp.tiered", "thesis.drift"} <= {
        item["kind"] for item in row["kinds"]
    }
    assert row["targets"] == ["symbol", "watchlist", "thesis", "position"]
    assert "market_rel" not in row["filters"]
    fixed = next(item for item in row["kinds"] if item["kind"] == "stop.fixed")
    assert fixed["required"] == ["price"]


def test_kinds_text_screen_reads_like_a_manual(container, capsys):
    code, text = out(container, capsys, "kinds")
    assert code == 0
    assert "stop.fixed（跌破绝对价即报）" in text
    assert "组合" not in text  # portfolio 进不了库（U28），屏幕上也不该出现它


# ---------- add ----------


def test_add_lands_the_shape_the_repo_validated(container, capsys):
    row = add_rule(container, capsys)
    assert row["params"] == {"price": 100000.0}
    assert row["severity"] == "strong" and row["enabled"] is True
    assert row["cooldown_min"] == 30 and row["targets"] == 1
    assert row["filters"] == dict.fromkeys(
        ["time_window", "data_freshness", "exclude_halt", "no_open_alert"], False
    )


def test_add_gives_a_default_name_from_the_template(container, capsys):
    code, row = payload(
        container, capsys, "add", "--user", USER, "--kind", "price.cross_above",
        "--target-type", "symbol", "--target-ref", f"A/{CODE}", "--param", "price=9",
    )
    assert code == 0 and row["rule"]["name"] == "上穿指定价"


def test_add_refuses_a_value_that_is_not_a_number_with_json(container, capsys):
    """D57：`--json` 的拒绝也得是 JSON。管道里读的人拿不到栈，只能拿到一句原因。"""
    code, row = payload(
        container, capsys, "add", "--user", USER, "--kind", "stop.fixed",
        "--target-type", "symbol", "--target-ref", f"A/{CODE}", "--param", "price=十块",
    )
    assert code == 2 and row["status"] == "refused"
    assert "要的是数字" in row["reason"]


def test_add_refuses_an_unknown_kind_by_naming_the_whole_list(container, capsys):
    code, row = payload(
        container, capsys, "add", "--user", USER, "--kind", "stop.magic",
        "--target-type", "symbol", "--target-ref", f"A/{CODE}", "--param", "price=10",
    )
    assert code == 2 and "未知规则类型" in row["reason"] and "tp.tiered" in row["reason"]


def test_add_refuses_a_missing_param_and_says_which(container, capsys):
    code, row = payload(
        container, capsys, "add", "--user", USER, "--kind", "stop.fixed",
        "--target-type", "symbol", "--target-ref", f"A/{CODE}",
    )
    assert code == 2 and "price" in row["reason"]


def test_add_refuses_a_malformed_param_pair(container, capsys):
    code, row = payload(
        container, capsys, "add", "--user", USER, "--kind", "stop.fixed",
        "--target-type", "symbol", "--target-ref", f"A/{CODE}", "--param", "price 10",
    )
    assert code == 2 and "k=v" in row["reason"]


def test_add_refuses_a_foreign_account_before_writing_anything(container, capsys):
    other = container.users.create("other", PWD)
    theirs = container.sim_accounts.open(other.id, "别人的户")
    code, row = payload(
        container, capsys, "add", "--user", USER, "--kind", "stop.pct",
        "--target-type", "position", "--target-ref", "*", "--account", theirs.id,
        "--param", "pct=0.1",
    )
    assert code == 2 and "名下没有账户" in row["reason"]
    assert kinds_of(container, capsys) == {}


def test_add_refuses_an_unknown_filter_by_name(container, capsys):
    code, row = payload(
        container, capsys, "add", "--user", USER, "--kind", "stop.fixed",
        "--target-type", "symbol", "--target-ref", f"A/{CODE}", "--param", "price=10",
        "--filter", "market_rel",
    )
    assert code == 2 and "没有输入" in row["reason"]


def test_add_off_builds_a_rule_that_no_run_will_pick_up(container, capsys):
    row = add_rule(container, capsys, "--off")
    assert row["enabled"] is False
    assert any("已停用" in note for note in row["notes"])


# ---------- list / show ----------


def test_list_on_an_empty_name_says_what_to_do_next(container, capsys):
    code, text = out(container, capsys, "list", "--user", USER)
    assert code == 0
    assert "没有规则" in text and "rule derive" in text


def test_list_counts_the_targets_and_marks_the_silent_one(container, capsys):
    add_rule(container, capsys)
    add_rule(container, capsys, "--name", "查无此码", code=BAD_CODE)
    code, text = out(container, capsys, "list", "--user", USER)
    assert code == 0
    assert "跌破十万" in text and "对象 1" in text


def test_show_prints_which_filters_are_on_and_why_it_cannot_fire(container, capsys):
    row = add_rule(container, capsys, "--filter", "data_freshness", code=BAD_CODE)
    code, text = out(container, capsys, "show", row["id"], "--user", USER)
    assert code == 0
    assert "data_freshness" in text and "全关" not in text
    assert "库里没有" in text  # `RuleService._notes` 那一句，原样上屏


# ---------- edit ----------


def test_edit_one_param_keeps_the_other_one(container, capsys):
    """命令层的按键覆盖：仓库是整包替换，改一个数时另一个不该悄悄回默认。"""
    account = open_account(container)
    code, row = payload(
        container, capsys, "add", "--user", USER, "--kind", "stop.time",
        "--target-type", "position", "--target-ref", "*", "--account", account,
        "--param", "days=60", "--param", "max_gain_pct=0.1",
    )
    assert code == 0 and row["rule"]["params"] == {"days": 60.0, "max_gain_pct": 0.1}
    code, got = payload(
        container, capsys, "edit", row["rule"]["id"], "--user", USER, "--param", "days=90"
    )
    assert code == 0
    assert got["rule"]["params"] == {"days": 90.0, "max_gain_pct": 0.1}


def test_edit_a_derived_stop_moves_the_thesis_column(container, capsys):
    thesis = make_thesis(container)
    derive(container, capsys, thesis.id)
    stop = kinds_of(container, capsys)["stop.fixed"]
    code, got = payload(
        container, capsys, "edit", stop["id"], "--user", USER, "--param", "price=30"
    )
    assert code == 0 and got["written_back"] == {"stop_price": 30.0}
    assert container.theses_store.get(uid(container), thesis.id).stop_price == 30.0


def test_edit_text_screen_says_which_column_it_wrote_back(container, capsys):
    thesis = make_thesis(container)
    derive(container, capsys, thesis.id)
    stop = kinds_of(container, capsys)["stop.fixed"]
    code, text = out(
        container, capsys, "edit", stop["id"], "--user", USER, "--param", "price=30"
    )
    assert code == 0
    assert "止损价 → 30" in text and "同一个数" in text


def test_a_write_back_refusal_leaves_the_rule_untouched(container, capsys):
    """那条事务的验收线：止损 60 会越过目标 52，论被拒 ⇒ 规则也回到 28。"""
    thesis = make_thesis(container)
    derive(container, capsys, thesis.id)
    stop = kinds_of(container, capsys)["stop.fixed"]
    with pytest.raises(ValidationError, match="不在目标"):
        container.rule_service.edit(uid(container), stop["id"], params={"price": 60.0})
    assert container.rules_store.get(stop["id"]).params["price"] == 28.0
    assert container.theses_store.get(uid(container), thesis.id).stop_price == 28.0


def test_edit_without_anything_to_change_is_refused(container, capsys):
    row = add_rule(container, capsys)
    code, got = payload(container, capsys, "edit", row["id"], "--user", USER)
    assert code == 2 and "什么都没要改" in got["reason"]


def test_edit_kind_without_new_params_is_refused(container, capsys):
    row = add_rule(container, capsys)
    code, got = payload(
        container, capsys, "edit", row["id"], "--user", USER, "--kind", "stop.pct"
    )
    assert code == 2 and "新的 params" in got["reason"]


# ---------- 启停与删除 ----------


def test_disable_then_enable_flips_the_rule(container, capsys):
    row = add_rule(container, capsys)
    assert out(container, capsys, "disable", row["id"], "--user", USER)[0] == 0
    assert payload(container, capsys, "list", "--user", USER, "--enabled-only")[1][
        "rules"
    ] == []
    assert payload(container, capsys, "enable", row["id"], "--user", USER)[0] == 0
    assert [item["id"] for item in kinds_of(container, capsys).values()] == [row["id"]]


def test_remove_wants_yes_and_says_what_it_takes_with_it(container, capsys):
    row = add_rule(container, capsys)
    code, got = payload(container, capsys, "remove", row["id"], "--user", USER)
    assert code == 2 and got["status"] == "refused" and "--yes" in got["reason"]
    assert kinds_of(container, capsys) != {}
    assert payload(container, capsys, "remove", row["id"], "--user", USER, "--yes")[0] == 0
    assert kinds_of(container, capsys) == {}


def test_a_derived_rule_cannot_be_removed_and_points_at_disable(container, capsys):
    """§9.2 那句「不可删除，只能停用」在命令行上的样子：拒绝里要给出那条能走的路。"""
    thesis = make_thesis(container)
    derive(container, capsys, thesis.id)
    stop = kinds_of(container, capsys)["stop.fixed"]
    code, got = payload(
        container, capsys, "remove", stop["id"], "--user", USER, "--yes"
    )
    assert code == 2
    assert "不能删除" in got["reason"] and "rule disable" in got["reason"]


# ---------- derive ----------


def test_derive_builds_the_stack_and_the_screen_lists_all_three_kinds(container, capsys):
    thesis = make_thesis(container)
    code, text = out(container, capsys, "derive", thesis.id, "--user", USER)
    assert code == 0
    assert "新建 5 条" in text and "没建 1 条" in text  # 没有账户 ⇒ 分批止盈那条不建
    assert "跌进买入区间" in text and "披露日复检" in text


def test_deriving_twice_creates_nothing_new(container, capsys):
    thesis = make_thesis(container)
    account = open_account(container)
    assert len(derive(container, capsys, thesis.id, account=account)["created"]) == 6
    again = derive(container, capsys, thesis.id, account=account)
    assert again["created"] == [] and len(again["already"]) == 6


def test_derive_names_the_tier_it_skipped_and_why(container, capsys):
    thesis = make_thesis(container)
    row = derive(container, capsys, thesis.id)
    skipped = {item["kind"]: item["reason"] for item in row["skipped"]}
    assert "tp.tiered" in skipped and "账户" in skipped["tp.tiered"]


# ---------- check ----------


def test_a_hit_pushes_and_returns_zero(container, capsys):
    """推了一条也是 0：退出码说的是这一趟有没有说清楚，不是有没有告警。"""
    add_rule(container, capsys)
    code, row = payload(container, capsys, "check", "--user", USER)
    assert code == 0 and len(row["raised"]) == 1 and row["judged"] == 1
    assert row["raised"][0]["stage"] == "hit" and row["raised"][0]["alert_id"]


def test_dry_run_pushes_nothing_but_says_it_would(container, capsys):
    add_rule(container, capsys)
    before = len(container.alerts_store.list_for(uid(container)))
    code, row = payload(container, capsys, "check", "--user", USER, "--dry-run")
    assert code == 0 and row["dry_run"] is True
    assert row["raised"][0]["alert_id"] == ""
    assert len(container.alerts_store.list_for(uid(container))) == before


def test_a_code_that_is_not_in_the_library_is_skipped_not_fatal(container, capsys):
    """判不了 ⇒ 1 并且说得出为什么：这与「没到」是两摞，屏幕上不能合成一摞。"""
    add_rule(container, capsys, code=BAD_CODE)
    code, row = payload(container, capsys, "check", "--user", USER)
    assert code == 1 and row["status"] == "incomplete"
    assert len(row["skipped"]) == 1 and "K 线" in row["skipped"][0]["reason"]
    assert row["raised"] == []


def test_one_bad_rule_does_not_stop_the_good_one(container, capsys):
    add_rule(container, capsys)
    add_rule(container, capsys, "--name", "查无此码", code=BAD_CODE)
    code, row = payload(container, capsys, "check", "--user", USER)
    assert code == 1  # 有判不了的，这一趟就不算说清楚
    assert len(row["raised"]) == 1 and len(row["skipped"]) == 1 and row["rules"] == 2


def test_check_with_no_rules_says_how_to_get_one(container, capsys):
    code, text = out(container, capsys, "check", "--user", USER)
    assert code == 0
    assert "规则 0 条" in text and "rule add" in text


def test_check_names_a_rule_that_is_not_hers(container, capsys):
    """`--rule` 点一条不存在的名：引擎只会回空列表，屏幕上就成了「跑了 0 条」。"""
    code, row = payload(container, capsys, "check", "--user", USER, "--rule", "no-such")
    assert code == 1 and row["status"] == "refused" and "不存在" in row["reason"]


def test_check_runs_a_disabled_rule_only_when_it_is_named(container, capsys):
    row = add_rule(container, capsys, "--off")
    code, got = payload(container, capsys, "check", "--user", USER, "--rule", row["id"])
    assert code == 0 and len(got["raised"]) == 1
    plain = payload(container, capsys, "check", "--user", USER)[1]
    assert plain["rules"] == 0 and plain["raised"] == []


def test_check_says_no_channel_when_the_push_went_nowhere(container, capsys):
    """库里那一行是真写进去了（它是这条告警的身份），但屏幕上要说它没出发。"""
    add_rule(container, capsys)
    code, text = out(container, capsys, "check", "--user", USER)
    assert code == 0 and "没有任何通道" in text and "channel add" in text


def test_dry_run_does_not_warn_about_channels(container, capsys):
    """「写进了库但没出发」只跟着真推出去的那些次说。

    不测「名下通道全部停用」那一款：库里到不了那个状态——第一条通道自动成为默认，
    而默认通道不许停用（`ChannelRepo.set_enabled` 那句），所以停用不掉的至少还剩一条。
    """
    add_rule(container, capsys)
    code, text = out(container, capsys, "check", "--user", USER, "--dry-run")
    assert code == 0 and "没有任何通道" not in text


# ---------- recheck ----------


def test_recheck_on_a_nameless_world_returns_zero_and_says_so(container, capsys):
    code, text = out(container, capsys, "recheck", "--user", USER)
    assert code == 0 and "没有 opened/tracking" in text


def test_recheck_lays_a_baseline_then_calls_a_drift_at_zero(container, capsys):
    """掉档是复检的正常出口：做成非零就是让 M3-D 为一次成功的复检重试。"""
    open_thesis(container)  # 还没有基线 ⇒ 这一趟要建一条
    # `set_baseline` 只写一次（0016）：一条论一旦被复检建了基线就再也抬不动它。
    # 所以「掉档」那一半另开一条论——换 style 才躲得开「同 (市场,代码,style) 只留一条活论」
    # 那条闸（内置打分模板只有 value/trend，所以这里只能用 trend）——趁它还没有基线时
    # 立一个 95 的高基准；现算仍是 68.96 ⇒ 掉一档。
    high = open_thesis(container, style="trend")
    container.theses_store.set_baseline(
        uid(container),
        high.id,
        score=95.0,
        items={"pr": 95.0, "roe": 95.0},
        on=date(2024, 7, 1),
        now=T0,
    )
    code, row = payload(container, capsys, "recheck", "--user", USER)
    assert code == 0 and row["counts"]["baseline"] == 1  # 第一条：这一趟只建基线
    code, text = out(container, capsys, "recheck", "--user", USER)
    assert code == 0 and "观点漂移" in text and "只提示" in text  # 第二条：95 → 68.96 掉档


def test_recheck_dry_run_writes_nothing(container, capsys):
    open_thesis(container)
    code, row = payload(container, capsys, "recheck", "--user", USER, "--dry-run")
    assert code == 0 and row["dry_run"] is True
    assert row["rows"] and all(item["written"] is False for item in row["rows"])


def test_recheck_of_an_unopened_thesis_is_refused_by_name(container, capsys):
    thesis = make_thesis(container)  # proposed：没有「建仓时那一眼」
    code, row = payload(
        container, capsys, "recheck", "--user", USER, "--thesis", thesis.id
    )
    assert code == 2 and "不在复检范围" in row["reason"]


def test_recheck_returns_one_when_the_score_did_not_come_out(db, settings, capsys):
    """没有基本面 ⇒ 没有分 ⇒ 比不了。这不能是 0：屏幕上那句要跟着说「这一趟没判」。"""
    sparse = make_container(db, settings, with_fundamentals=False)
    sparse.users.create(USER, PWD)
    seed_calendar(sparse)
    thesis = make_thesis(sparse)
    sparse.theses_store.advance(
        uid(sparse), thesis.id, "opened", actor_id=uid(sparse), now=T0,
    )
    code, row = payload(sparse, capsys, "recheck", "--user", USER)
    assert code == 1 and row["counts"]["no_score"] == 1
