"""thesis 的用例层（M2-E，spec §9.1）：状态机、当前估值、那条时间线接成一屏。

这一层不算任何东西（式子在 `analysis/valuation`，状态表在 `storage/repos/theses`），
它只决定三件事，而三处都会静默出错：

1. **立论时那几列价位从哪来**。当场跑一次估值，只把**它给得出的**那几列落库：给不出时
   补一个 0 或写一个 None，屏幕上都是一个「有记录的价位」，而它没有任何式子 backing。
2. **`show` 对照的是两次测量**。库里那几列写于立论那一天，当前这一份按今天的库重算，
   两个数不等并不等于「观点变了」——报告要说清是哪两次测量，不能替用户下一个漂移的结论
   （周复检的漂移判定是 §9.3 / M3 的事，它需要的是「建仓时那份快照」，这里还没存）。
3. **谁能读写这一条**。用户名先解析成 `user_id` 才交给仓库；不存在与已禁用是两种拒绝，
   而别人的 thesis 连时间线都读不到（那条不变式在仓库层，这里只验接线没漏）。
"""

from __future__ import annotations

import pytest
from support_valuation import CODE, make_container

from sams.core.errors import StorageError, ValidationError
from sams.storage.repos.theses import (
    ARCHIVED,
    INVALIDATED,
    OBSERVING,
    OPENED,
    PROPOSED,
    TRACKING,
)

USER = "trader"
OTHER = "watcher"
PWD = "correct-horse-battery"


def build(db, settings, **seeded):
    """一份铺好的样本库 + 一个能落笔的用户。"""
    container = make_container(db, settings, **seeded)
    container.users.create(USER, PWD)
    return container


def levels(thesis) -> dict:
    return {
        column: getattr(thesis, column)
        for column in ("entry_low", "entry_high", "stop_price", "target_price", "reward_risk")
    }


def row(report, key: str):
    found = [item for item in report.rows if item.key == key]
    assert found, f"{key} 不在对照表里：{[item.key for item in report.rows]}"
    return found[0]


# --- 立论 ----------------------------------------------------------------------------------


def test_add_stores_exactly_the_levels_the_valuation_gave(db, settings):
    """`add` 落的那几列必须等于当场那条结论给的那几个数，一位小数都不差。"""
    container = build(db, settings)
    plan = container.valuation.read("A", CODE).plan
    report = container.theses.add(USER, "A", CODE)
    thesis = report.thesis
    assert thesis.status == OBSERVING
    assert thesis.style == "value"
    assert thesis.market == "A" and thesis.code == CODE
    assert thesis.entry_low == pytest.approx(plan.entry.low)
    assert thesis.entry_high == pytest.approx(plan.entry.high)
    assert thesis.entry_low != thesis.entry_high  # 区间不是同一个数抄两遍
    assert thesis.stop_price == pytest.approx(plan.stop.low)
    assert thesis.target_price == pytest.approx(plan.target.low)
    assert thesis.reward_risk == pytest.approx(plan.odds.value)
    assert report.unavailable is None
    assert report.valuation is not None


def test_add_starts_proposed_when_asked(db, settings):
    """`proposed` 是「已经想过要买」：状态由调用方给，但只有链条开头那两个能用。"""
    container = build(db, settings)
    thesis = container.theses.add(USER, "A", CODE, status=PROPOSED, note="等回调").thesis
    assert thesis.status == PROPOSED
    assert container.theses.show(USER, thesis.id).events[0].note == "等回调"


def test_add_refuses_a_style_that_has_no_template(db, settings):
    """风格名要在落库前就查：一条存着读不出来的模板名的 thesis，每次 `show` 都会崩。"""
    container = build(db, settings)
    with pytest.raises(ValidationError, match="模板"):
        container.theses.add(USER, "A", CODE, style="momentum")
    assert container.theses.list(USER) == []


def test_add_refuses_a_name_that_is_not_a_user(db, settings):
    container = build(db, settings)
    with pytest.raises(ValidationError, match="sams user add"):
        container.theses.add("ghost", "A", CODE)


def test_a_disabled_user_cannot_write_or_read(db, settings):
    """禁用之后还能立论，那条「禁用即停止一切动作」就是纸面的。"""
    container = build(db, settings)
    container.users.create(OTHER, PWD)
    container.users.disable(container.users.get_by_username(OTHER).id)
    with pytest.raises(ValidationError, match="禁用"):
        container.theses.add(OTHER, "A", CODE)
    with pytest.raises(ValidationError, match="禁用"):
        container.theses.list(OTHER)


def test_a_second_live_thesis_on_the_same_stock_is_refused_and_names_the_first(db, settings):
    """同一个票同一套模板留两条活的，M3 的派生规则会翻倍，而回看时不知道哪条是当时的决定。"""
    container = build(db, settings)
    first = container.theses.add(USER, "A", CODE).thesis
    with pytest.raises(ValidationError, match=first.id):
        container.theses.add(USER, "A", CODE)
    assert len(container.theses.list(USER)) == 1


def test_archiving_frees_the_slot(db, settings):
    """归档了的就是历史：同一只票可以再开一条，两条各自带自己的时间线。"""
    container = build(db, settings)
    thesis = container.theses.add(USER, "A", CODE).thesis
    container.theses.advance(USER, thesis.id, INVALIDATED, note="逻辑变了")
    container.theses.advance(USER, thesis.id, ARCHIVED)
    assert container.theses.add(USER, "A", CODE).thesis.status == OBSERVING
    assert len(container.theses.list(USER)) == 2


def test_the_same_style_on_a_different_stock_is_not_a_duplicate(db, settings):
    container = build(db, settings)
    container.theses.add(USER, "A", CODE)
    assert container.theses.add(USER, "A", "600519").thesis.code == "600519"


def test_add_still_records_the_thesis_when_no_price_can_be_computed(db, settings):
    """库里没有日线时也算不出价位：thesis 照记（观点不必等数据），但那几列留空且要说为什么。"""
    container = build(db, settings, with_bars=False)
    report = container.theses.add(USER, "A", CODE)
    assert report.thesis.status == OBSERVING
    assert levels(report.thesis) == dict.fromkeys(levels(report.thesis))
    assert report.valuation is None
    assert "K 线" in report.unavailable
    assert "sams sync daily" in report.unavailable


# --- 对照：库里那次 vs 现在这次 --------------------------------------------------------------


def test_the_report_pairs_each_stored_level_with_the_current_one(db, settings):
    container = build(db, settings)
    thesis = container.theses.add(USER, "A", CODE).thesis
    report = container.theses.show(USER, thesis.id)
    assert [item.key for item in report.rows] == ["entry", "stop", "target", "odds"]
    for item in report.rows:
        assert item.stored == item.current
        assert "一致" in item.note


def test_the_gap_between_the_two_measurements_is_printed_as_a_gap(db, settings):
    """手填过一个价位之后，`show` 要把「库里那个」与「现在算出的那个」并排给出来。"""
    container = build(db, settings)
    thesis = container.theses.add(USER, "A", CODE).thesis
    container.theses.revise(USER, thesis.id, stop_price=9.99)
    report = container.theses.show(USER, thesis.id)
    stop = row(report, "stop")
    assert stop.stored == pytest.approx((9.99, 9.99))
    assert stop.current[0] is not None and stop.current[0] > 9.99
    assert "%" in stop.note


def test_a_level_the_current_plan_cannot_compute_does_not_erase_the_stored_one(db, settings):
    """没有基本面 ⇒ 买入区算不出。此时 `--from-plan` 既不补一个数，也不把手填的那对抹成空。"""
    container = build(db, settings, with_fundamentals=False)
    thesis = container.theses.add(USER, "A", CODE).thesis
    assert thesis.entry_low is None
    hand = container.theses.revise(USER, thesis.id, entry_low=11.0, entry_high=12.0)
    after = container.theses.revise(USER, hand.thesis.id, from_plan=True)
    assert after.thesis.entry_low == 11.0
    assert after.thesis.entry_high == 12.0
    # 而止损这条当前算得出，于是它被写进来了
    assert after.thesis.stop_price == pytest.approx(after.valuation.plan.stop.low)
    assert row(after, "entry").stored == (11.0, 12.0)


def test_the_report_says_which_measurement_each_number_is_from(db, settings):
    """两栏的日期要印出来：把「今天算的」读成「当时定的」，回看时的结论就是反的。"""
    container = build(db, settings)
    thesis = container.theses.add(USER, "A", CODE).thesis
    report = container.theses.show(USER, thesis.id)
    assert any("库里那次" in note for note in report.notes)
    assert any(str(report.valuation.as_of) in note for note in report.notes)


# --- 现价落在计划哪一段 ---------------------------------------------------------------------


def test_the_report_says_where_the_price_sits_against_the_plan(db, settings):
    """样本里现价 17 在买入区上限之上：那句「还没到」要说得出，因为它是计划本身的条件。"""
    container = build(db, settings)
    thesis = container.theses.add(USER, "A", CODE).thesis
    report = container.theses.show(USER, thesis.id)
    assert "买入区上限" in report.placement
    assert "17" in report.placement


def test_crossing_the_stored_stop_names_the_jump_that_matches(db, settings):
    container = build(db, settings)
    thesis = container.theses.add(USER, "A", CODE).thesis
    container.theses.revise(USER, thesis.id, stop_price=99.0)
    report = container.theses.show(USER, thesis.id)
    assert "止损" in report.placement
    # 那句下一步要能当场敲出去：带着 id，也带着 `--user`（它是必填的）
    assert f"thesis advance {thesis.id} --user {USER}" in report.placement
    assert "--to hit_stop" in report.placement


def test_crossing_the_stored_target_names_hit_target(db, settings):
    container = build(db, settings)
    thesis = container.theses.add(USER, "A", CODE).thesis
    container.theses.revise(USER, thesis.id, target_price=1.0)
    report = container.theses.show(USER, thesis.id)
    assert "目标" in report.placement
    assert "hit_target" in report.placement


def test_a_thesis_without_levels_says_so_instead_of_no_whereabouts(db, settings):
    container = build(db, settings, with_bars=False)
    thesis = container.theses.add(USER, "A", CODE).thesis
    report = container.theses.show(USER, thesis.id)
    assert "还没有价位" in report.placement
    assert "sams thesis revise" in report.placement


# --- 接线：归属、时间线、下一步 --------------------------------------------------------------


def test_another_users_thesis_is_not_even_readable(db, settings):
    container = build(db, settings)
    container.users.create(OTHER, PWD)
    thesis = container.theses.add(USER, "A", CODE).thesis
    with pytest.raises(StorageError):
        container.theses.show(OTHER, thesis.id)
    with pytest.raises(StorageError):
        container.theses.advance(OTHER, thesis.id, PROPOSED)
    assert container.theses.list(OTHER) == []


def test_the_timeline_records_who_moved_it(db, settings):
    container = build(db, settings)
    thesis = container.theses.add(USER, "A", CODE).thesis
    container.theses.advance(USER, thesis.id, PROPOSED, note="准备买")
    report = container.theses.show(USER, thesis.id)
    events = report.events
    assert [(e.from_status, e.to_status) for e in events] == [
        (None, OBSERVING),
        (OBSERVING, PROPOSED),
    ]
    assert events[1].actor_id == container.users.get_by_username(USER).id
    assert events[1].note == "准备买"


def test_next_steps_comes_from_the_same_table_the_repo_uses(db, settings):
    """命令行要列「接下来能走哪」，那份名单必须与仓库拒绝时用的是同一张表。"""
    container = build(db, settings)
    thesis = container.theses.add(USER, "A", CODE).thesis
    assert container.theses.next_steps(thesis.status) == (PROPOSED, INVALIDATED)
    moved = container.theses.advance(USER, thesis.id, PROPOSED)
    assert container.theses.next_steps(moved.status) == (OPENED, INVALIDATED)


def test_opening_without_levels_names_a_command_that_exists(db, settings):
    """拒绝的话里那条下一步命令要真能敲：指一个不存在的子命令等于没指。"""
    container = build(db, settings, with_bars=False)
    thesis = container.theses.add(USER, "A", CODE).thesis
    container.theses.advance(USER, thesis.id, PROPOSED)
    with pytest.raises(ValidationError, match="sams thesis revise"):
        container.theses.advance(USER, thesis.id, OPENED)


def test_revise_rejects_a_column_that_is_not_a_level(db, settings):
    """列名写错时不能静默忽略：用户以为改了止损，库里它还是空的。"""
    container = build(db, settings)
    thesis = container.theses.add(USER, "A", CODE).thesis
    with pytest.raises(ValidationError, match="stop_pirce"):
        container.theses.revise(USER, thesis.id, stop_pirce=1.0)


def test_list_passes_the_status_filter_through(db, settings):
    container = build(db, settings)
    thesis = container.theses.add(USER, "A", CODE).thesis
    container.theses.advance(USER, thesis.id, PROPOSED)
    container.theses.advance(USER, thesis.id, OPENED)
    container.theses.advance(USER, thesis.id, TRACKING)
    assert [item.status for item in container.theses.list(USER, status=TRACKING)] == [TRACKING]
    assert container.theses.list(USER, status=PROPOSED) == []
