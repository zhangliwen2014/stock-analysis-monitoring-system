"""`sams thesis` 的出口契约（M2-E，spec §9.1）。

验的是屏幕上那几行有没有替这条记录说假话，不验状态机（在 `tests/storage`）也不验对照表
怎么拼（在 `tests/services`）：

* **每个价位都要带着它的出处**。立论那一屏把四个价位干巴巴列出来，读的人分不清这是
  当场算的还是手填的；`--json` 里也一样，`from` 那一句要说它是从哪次测量落下来的。
* **拒绝也得是 JSON**（D57）。参数错、跳不了的那一步、库里没数据，在管道里都该有可解析的
  一行加一个 `exit`，而不是 stderr 上一段纯文本配一个空 stdin。
* **屏幕上要有下一步那条命令**，且它真能敲（同一条红线在 M2-A/C 被踩过好几次）。
* **一行一条记录**。`list` 把整份结论复述一遍，20 条 thesis 就刷不出下一页。
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from support_valuation import CODE, make_container

import sams.cli as cli

USER = "trader"
PWD = "correct-horse-battery"
#: 往后倒还得留出够长的分位窗口：2024-09-30 是「倒得回去、四栏还算得出」的那一类，
#: 2024-06-30 则已经掉到样本地板（250 个可比交易日）以下 ⇒ 买入区与目标给不出（见下面那两条用例）
BACK = date(2024, 9, 30)
TOO_FAR = date(2024, 6, 30)


def build(db, settings, **seeded) -> object:
    container = make_container(db, settings, **seeded)
    container.users.create(USER, PWD)
    return container


def invoke(container, *extra: str) -> int:
    args = cli.build_parser().parse_args(["thesis", *extra])
    return args.handler(container, args)


def out(container, capsys, *extra: str) -> tuple[int, str]:
    capsys.readouterr()
    code = invoke(container, *extra)
    return code, capsys.readouterr().out


def payload(container, capsys, *extra: str) -> tuple[int, dict]:
    code, text = out(container, capsys, *extra, "--json")
    return code, json.loads(text)


def add(container, capsys, *extra: str, code: int = 0) -> str:
    """立一条论，把它的 id 交出来（后面的用例都要拿它当参数）。

    `code` 默认 0：一只数据齐的票立论就该给出完整结论。数据缺的那几种要显式传 1，
    这样「这条 thesis 没有价位」是用例说出来的，而不是被一个宽松的帮助函数咽下去。
    """
    status, body = out(
        container, capsys, "add", "--user", USER, "--market", "A", "--code", CODE, *extra
    )
    assert status == code, body
    return body.split("id ")[1].split()[0]


# --- add -----------------------------------------------------------------------------------


def test_add_prints_the_levels_it_stored_and_where_they_came_from(db, settings, capsys):
    container = build(db, settings)
    code, body = out(container, capsys, "add", "--user", USER, "--market", "A", "--code", CODE)
    assert code == 0
    for label in ("买入区", "止损", "目标", "赔率"):
        assert label in body, f"{label} 没出现在屏幕上"
    assert "当场算出" in body
    assert "sams thesis advance" in body


def test_add_json_carries_the_thesis_the_levels_and_the_exit(db, settings, capsys):
    container = build(db, settings)
    code, data = payload(
        container, capsys, "add", "--user", USER, "--market", "A", "--code", CODE
    )
    assert code == 0 and data["exit"] == 0
    thesis = data["thesis"]
    assert thesis["status"] == "observing" and thesis["style"] == "value"
    assert thesis["stop_price"] == pytest.approx(data["valuation"]["plan"]["stop"][0])
    assert thesis["id"]


def test_an_unknown_user_is_exit_two_in_json_too(db, settings, capsys):
    """参数错也走 JSON（D57）：管道里读的人拿到的是一句原因，不是一个空 stdin。"""
    container = build(db, settings)
    code, data = payload(container, capsys, "list", "--user", "ghost")
    assert code == 2 and data["exit"] == 2
    assert data["status"] == "refused"
    assert "sams user add" in data["reason"]


def test_add_refuses_a_second_live_thesis_on_the_same_stock(db, settings, capsys):
    container = build(db, settings)
    first = add(container, capsys)
    code, data = payload(
        container, capsys, "add", "--user", USER, "--market", "A", "--code", CODE
    )
    assert code == 2
    assert data["status"] == "refused"
    assert first in data["reason"]


def test_add_on_a_stock_without_bars_still_records_the_view_but_exits_one(db, settings, capsys):
    """没有日线就没有价位：thesis 记下来（观点不该等数据），但这一屏没给出一条完整结论。"""
    container = build(db, settings, with_bars=False)
    code, body = out(container, capsys, "add", "--user", USER, "--market", "A", "--code", CODE)
    assert code == 1
    assert "价位" in body and "空着" in body
    assert "sams sync daily" in body


def test_add_honours_as_of_and_the_roe_caliber(db, settings, capsys):
    """`--as-of` 要把四个价位、分位窗口、PIT 闸门一起带回那天：屏幕上要说用的哪一天。"""
    container = build(db, settings)
    code, body = out(
        container,
        capsys,
        "add",
        "--user",
        USER,
        "--market",
        "A",
        "--code",
        CODE,
        "--as-of",
        str(BACK),
    )
    assert code == 0, body
    assert str(BACK) in body


def test_add_back_beyond_the_percentile_floor_exits_one(db, settings, capsys):
    """倒得太远，分位窗口的样本掉到地板（250 个可比交易日）以下：买入区与目标给不出。

    这时立论照样记进库（观点不该等数据），但退出码必须是 1——屏幕上四栏里两栏空着，
    而「立论成功」与「立论记下了、价位只有一半」在脚本里是两件事。
    """
    container = build(db, settings)
    code, body = out(
        container,
        capsys,
        "add",
        "--user",
        USER,
        "--market",
        "A",
        "--code",
        CODE,
        "--as-of",
        str(TOO_FAR),
    )
    assert code == 1
    assert str(TOO_FAR) in body
    assert "少于地板" in body or "算不出" in body
    assert "止损" in body  # 给得出的那一栏照样落库，也在屏幕上列出来


def test_add_rejects_a_percent_written_ratio(db, settings, capsys):
    container = build(db, settings)
    code, body = out(
        container,
        capsys,
        "add",
        "--user",
        USER,
        "--market",
        "A",
        "--code",
        CODE,
        "--roe-estimate",
        "15",
    )
    assert code == 2
    assert "0.15" in body


# --- list ----------------------------------------------------------------------------------


def test_an_empty_list_is_not_an_error(db, settings, capsys):
    """名下没有 thesis 是正常结果：退出码 0，屏幕上给一条能敲的下一步。"""
    container = build(db, settings)
    code, body = out(container, capsys, "list", "--user", USER)
    assert code == 0
    assert "sams thesis add" in body


def test_list_prints_one_line_per_thesis(db, settings, capsys):
    container = build(db, settings)
    thesis_id = add(container, capsys)
    code, body = out(container, capsys, "list", "--user", USER)
    assert code == 0
    lines = [line for line in body.splitlines() if CODE in line]
    assert len(lines) == 1
    assert "observing" in lines[0] and thesis_id[:8] in lines[0]


def test_list_can_show_just_one_status(db, settings, capsys):
    container = build(db, settings)
    thesis_id = add(container, capsys)
    out(container, capsys, "advance", thesis_id, "--user", USER, "--to", "proposed")
    code, body = out(container, capsys, "list", "--user", USER, "--status", "proposed")
    assert code == 0 and "proposed" in body
    code, body = out(container, capsys, "list", "--user", USER, "--status", "opened")
    assert code == 0 and "没有" in body


def test_list_rejects_an_unknown_status_before_reading(db, settings, capsys):
    container = build(db, settings)
    code, data = payload(container, capsys, "list", "--user", USER, "--status", "half_open")
    assert code == 2
    assert "observing" in data["reason"]


# --- show ----------------------------------------------------------------------------------


def test_show_prints_the_two_measurements_side_by_side(db, settings, capsys):
    container = build(db, settings)
    thesis_id = add(container, capsys)
    code, body = out(container, capsys, "show", thesis_id, "--user", USER)
    assert code == 0
    assert "库里那次" in body and "当前这次" in body
    assert "现价" in body
    assert "时间线" in body


def test_show_points_at_the_command_that_produces_the_full_trace(db, settings, capsys):
    """屏幕上只放摘要；「每个分数都能追到来源」由 `analysis valuation` 那一屏满足。"""
    container = build(db, settings)
    thesis_id = add(container, capsys)
    code, body = out(container, capsys, "show", thesis_id, "--user", USER)
    assert code == 0
    assert "sams analysis valuation" in body
    assert "--style value" in body


def test_show_json_carries_events_and_the_comparison(db, settings, capsys):
    container = build(db, settings)
    thesis_id = add(container, capsys)
    out(container, capsys, "advance", thesis_id, "--user", USER, "--to", "proposed")
    code, data = payload(container, capsys, "show", thesis_id, "--user", USER)
    assert code == 0 and data["exit"] == 0
    assert [(e["from"], e["to"]) for e in data["events"]] == [
        (None, "observing"),
        ("observing", "proposed"),
    ]
    assert [item["key"] for item in data["comparison"]] == ["entry", "stop", "target", "odds"]
    assert data["next_steps"] == ["opened", "invalidated"]
    assert data["valuation"]["composite"]["score"] is not None


def test_show_exits_one_when_there_is_no_current_valuation(db, settings, capsys):
    container = build(db, settings, with_bars=False)
    thesis_id = add(container, capsys, code=1)
    code, body = out(container, capsys, "show", thesis_id, "--user", USER)
    assert code == 1
    assert "算不出" in body


def test_show_cannot_read_another_users_record(db, settings, capsys):
    container = build(db, settings)
    container.users.create("watcher", PWD)
    thesis_id = add(container, capsys)
    code, data = payload(container, capsys, "show", thesis_id, "--user", "watcher")
    assert code == 1
    assert data["status"] == "refused"


# --- advance -------------------------------------------------------------------------------


def test_a_legal_jump_prints_the_new_status_and_what_comes_next(db, settings, capsys):
    container = build(db, settings)
    thesis_id = add(container, capsys)
    code, body = out(
        container,
        capsys,
        "advance",
        thesis_id,
        "--user",
        USER,
        "--to",
        "proposed",
        "--note",
        "想买了",
    )
    assert code == 0
    assert "proposed" in body
    assert "opened" in body  # 接下来能走哪，来自仓库那张表


def test_archiving_an_unopened_view_is_refused_and_changes_nothing(db, settings, capsys):
    """未建仓的观点没有可了结的仓位：拒掉之后状态得原样站着。"""
    container = build(db, settings)
    thesis_id = add(container, capsys)
    code, body = out(container, capsys, "advance", thesis_id, "--user", USER, "--to", "archived")
    assert code == 1
    assert "归档" in body
    _, after = out(container, capsys, "show", thesis_id, "--user", USER)
    assert "observing" in after


def test_an_unknown_status_is_a_two_not_a_one(db, settings, capsys):
    """认不出的状态名是用户打错了（2），走得通但被状态机挡住才是业务拒绝（1）。

    拒绝的话里要列出整套词表：只说「不合法」，用户只能再猜一次，而猜错的样子像数据坏了。
    """
    container = build(db, settings)
    thesis_id = add(container, capsys)
    code, data = payload(container, capsys, "advance", thesis_id, "--user", USER, "--to", "yolo")
    assert code == 2 and data["exit"] == 2
    for name in (
        "observing",
        "proposed",
        "opened",
        "tracking",
        "hit_target",
        "hit_stop",
        "invalidated",
        "archived",
    ):
        assert name in data["reason"]


def test_opening_is_refused_until_the_levels_are_in(db, settings, capsys):
    container = build(db, settings, with_bars=False)
    thesis_id = add(container, capsys, code=1)
    out(container, capsys, "advance", thesis_id, "--user", USER, "--to", "proposed")
    code, body = out(container, capsys, "advance", thesis_id, "--user", USER, "--to", "opened")
    assert code == 2
    assert "sams thesis revise" in body


def test_the_whole_lifecycle_walks_on_the_cli(db, settings, capsys):
    container = build(db, settings)
    thesis_id = add(container, capsys)
    for step in ("proposed", "opened", "tracking", "hit_target", "archived"):
        code, body = out(container, capsys, "advance", thesis_id, "--user", USER, "--to", step)
        assert code == 0, f"{step}: {body}"
    code, data = payload(container, capsys, "show", thesis_id, "--user", USER)
    assert code == 0
    assert data["thesis"]["status"] == "archived"
    assert data["thesis"]["archived_at"] is not None
    assert len(data["events"]) == 6


# --- revise --------------------------------------------------------------------------------


def test_revise_writes_the_hand_set_levels(db, settings, capsys):
    container = build(db, settings)
    thesis_id = add(container, capsys)
    code, data = payload(
        container,
        capsys,
        "revise",
        thesis_id,
        "--user",
        USER,
        "--stop",
        "15.0",
        "--target",
        "20.0",
        "--odds",
        "1.8",
    )
    assert code == 0
    assert data["thesis"]["stop_price"] == 15.0
    assert data["thesis"]["reward_risk"] == 1.8
    assert {"止损", "目标", "赔率"} <= set(data["written"])


def test_revising_a_price_to_zero_is_refused_as_a_two(db, settings, capsys):
    container = build(db, settings)
    thesis_id = add(container, capsys)
    code, data = payload(container, capsys, "revise", thesis_id, "--user", USER, "--stop", "0")
    assert code == 2
    assert data["status"] == "refused"


def test_revise_from_plan_writes_only_the_levels_the_plan_gives(db, settings, capsys):
    """没有基本面 ⇒ 买入区算不出：`--from-plan` 不该编一个数，也不该把手填的那对抹掉。"""
    container = build(db, settings, with_fundamentals=False)
    thesis_id = add(container, capsys)
    out(
        container,
        capsys,
        "revise",
        thesis_id,
        "--user",
        USER,
        "--entry-low",
        "11",
        "--entry-high",
        "12",
    )
    code, data = payload(container, capsys, "revise", thesis_id, "--user", USER, "--from-plan")
    assert code == 0
    assert data["thesis"]["entry_low"] == 11.0
    assert data["thesis"]["stop_price"] == pytest.approx(data["valuation"]["plan"]["stop"][0])
    assert "止损" in data["written"]


def test_revise_with_nothing_to_change_says_so(db, settings, capsys):
    """一个字段都没给时不能假装改了什么：静默返回旧记录，用户以为止损已经写进去了。"""
    container = build(db, settings)
    thesis_id = add(container, capsys)
    code, body = out(container, capsys, "revise", thesis_id, "--user", USER)
    assert code == 2
    assert "--from-plan" in body
