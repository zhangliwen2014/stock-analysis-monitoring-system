"""`theses` 与 `thesis_events` 的仓库层：状态机、时间线、用户隔离（spec §9.1）。

这一层的存在理由只有一条：**非法跳转在 storage 里就拒**，而不是靠每个调用方记得先查状态。
§14 M2 第 5 条点名的就是这件事（「未建仓直接归档」必须被拒），而归档那条路一旦能走通，
留下的是一条看起来正常的假时间线——回看「我当时为什么买」时，一个从未建仓的观点
带着一个 `archived` 状态，读的人无从分辨。

每次变更写一条 `thesis_events` 也是同一件事的另一半：状态是「现在是什么」，
事件是「什么时候从什么变来的、谁改的、说了什么」，后者才是回看时能用的东西。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sams.core.errors import StorageError, ValidationError
from sams.storage.repos.theses import (
    ARCHIVED,
    HIT_STOP,
    HIT_TARGET,
    INVALIDATED,
    OBSERVING,
    OPENED,
    PROPOSED,
    TRACKING,
    ThesisRepo,
)

T0 = datetime(2024, 7, 1, 9, 30, tzinfo=UTC)
T1 = datetime(2024, 7, 8, 9, 30, tzinfo=UTC)
T2 = datetime(2024, 8, 1, 9, 30, tzinfo=UTC)

USER = "u-1"
OTHER = "u-2"


def make(db, **over):
    """一条能走完全程的 thesis：默认在 `observing`，价位空着（建仓那一步才要求齐备）。"""
    repo = ThesisRepo(db)
    args = {
        "user_id": USER,
        "market": "A",
        "code": "601088",
        "style": "value",
        "actor_id": USER,
        "now": T0,
    }
    args.update(over)
    return repo.create(**args)


def walk(repo, thesis, *steps, **prices):
    """按顺序推进；每步的 `now` 往后推一天，好让时间线本身可比较。

    价位在这里先落一次：建仓那一步要求止损/目标/赔率齐备，而它们不是状态的一部分。
    """
    if prices:
        repo.set_levels(USER, thesis.id, actor_id=USER, now=thesis.updated_at, **prices)
    for i, to in enumerate(steps, start=1):
        thesis = repo.advance(
            USER, thesis.id, to, actor_id=USER, note=f"第 {i} 步", now=T0.replace(day=1 + i)
        )
    return thesis


# --- 开局与时间线 ---------------------------------------------------------------------------


def test_a_new_thesis_starts_as_observing_and_writes_the_first_event(db):
    """`observing` 是唯一的开局（spec §9.1 那条链的最左端），且第一跳也进时间线。"""
    thesis = make(db)
    assert thesis.status == OBSERVING
    assert thesis.archived_at is None
    assert (thesis.market, thesis.code) == ("A", "601088")
    events = ThesisRepo(db).events(USER, thesis.id)
    assert [(e.from_status, e.to_status) for e in events] == [(None, OBSERVING)]
    assert events[0].actor_id == USER
    assert events[0].ts == T0


def test_create_refuses_to_start_in_the_middle_of_the_chain(db):
    """直接从 `opened` 开局等于跳过「这条观点是怎么来的」，而那是这整张表的意义。"""
    with pytest.raises(ValidationError, match="observing|proposed"):
        make(db, status=OPENED)


def test_every_transition_appends_one_event(db):
    """六步走完就应有六条事件，`from_status` 接得上前一条的 `to_status`。"""
    repo = ThesisRepo(db)
    thesis = make(db)
    thesis = walk(
        repo,
        thesis,
        PROPOSED,
        OPENED,
        TRACKING,
        HIT_TARGET,
        ARCHIVED,
        entry_low=30.0,
        entry_high=33.0,
        stop_price=27.0,
        target_price=40.0,
        reward_risk=2.5,
    )
    assert thesis.status == ARCHIVED
    events = repo.events(USER, thesis.id)
    assert [e.to_status for e in events] == [
        OBSERVING,
        PROPOSED,
        OPENED,
        TRACKING,
        HIT_TARGET,
        ARCHIVED,
    ]
    assert [(e.from_status, e.to_status) for e in events[1:]] == [
        (events[i].to_status, events[i + 1].to_status) for i in range(len(events) - 1)
    ]
    assert events[1].note == "第 1 步"


def test_archiving_stamps_archived_at_and_leaving_it_open_does_not(db):
    """`archived_at` 只在真的归档那一步落，回看时「哪一条已经了结」不能靠读状态猜。"""
    repo = ThesisRepo(db)
    thesis = make(db)
    assert thesis.archived_at is None
    thesis = walk(
        repo,
        thesis,
        PROPOSED,
        OPENED,
        TRACKING,
        HIT_STOP,
        ARCHIVED,
        stop_price=27.0,
        target_price=40.0,
        reward_risk=2.5,
    )
    assert thesis.status == ARCHIVED
    assert thesis.archived_at is not None


# --- 非法跳转：在仓库层就拒 ------------------------------------------------------------------


def test_archiving_an_unopened_thesis_is_refused(db):
    """§14 M2 第 5 条点名的那一条：没建过仓的观点没有「了结」可言。"""
    repo = ThesisRepo(db)
    thesis = make(db)
    with pytest.raises(StorageError, match="归档"):
        repo.advance(USER, thesis.id, ARCHIVED, actor_id=USER, now=T1)
    assert repo.get(USER, thesis.id).status == OBSERVING, "拒绝不能顺手改掉状态"


def test_opened_requires_stop_target_and_odds(db):
    """建仓那一步是这条链上唯一要求「价位齐备」的：缺哪几个就点名哪几个。"""
    repo = ThesisRepo(db)
    thesis = make(db)
    thesis = repo.advance(USER, thesis.id, PROPOSED, actor_id=USER, now=T1)
    with pytest.raises(ValidationError, match="止损.*目标.*赔率"):
        repo.advance(USER, thesis.id, OPENED, actor_id=USER, now=T2)


def test_a_terminal_archived_thesis_cannot_be_reopened(db):
    """归档是终点。要再看这只票就另开一条：同一 id 上的第二次生命周期会把时间线读成噪声。"""
    repo = ThesisRepo(db)
    thesis = make(db)
    thesis = walk(
        repo,
        thesis,
        PROPOSED,
        OPENED,
        TRACKING,
        HIT_TARGET,
        ARCHIVED,
        stop_price=27.0,
        target_price=40.0,
        reward_risk=2.5,
    )
    with pytest.raises(StorageError, match="archived"):
        repo.advance(USER, thesis.id, TRACKING, actor_id=USER, now=T2)


def test_skipping_a_state_is_refused(db):
    """observing 直接到 tracking：中间那两步（提出、建仓）没有发生过。"""
    repo = ThesisRepo(db)
    thesis = make(db)
    with pytest.raises(StorageError, match=PROPOSED):
        repo.advance(USER, thesis.id, TRACKING, actor_id=USER, now=T1)


def test_an_unlisted_status_is_refused_before_it_reaches_the_table(db):
    """未知状态名与「已建仓又被强平」那种没定义过的跳转都拒：状态词表只有一份。"""
    repo = ThesisRepo(db)
    thesis = make(db)
    with pytest.raises(ValidationError):
        repo.advance(USER, thesis.id, "half_open", actor_id=USER, now=T1)


def test_the_same_state_twice_is_refused(db):
    """原地踏步不是一次变更：收下它就会在时间线上留一条「从 observing 到 observing」的噪声。"""
    repo = ThesisRepo(db)
    thesis = make(db)
    with pytest.raises(StorageError):
        repo.advance(USER, thesis.id, OBSERVING, actor_id=USER, now=T1)


def test_invalidation_is_reachable_from_every_live_state(db):
    """看错了可以在任何时刻承认：四个还没了结的状态都能进 `invalidated`，进去之后才归档。"""
    repo = ThesisRepo(db)
    reached = []
    for steps in (
        (),
        (PROPOSED,),
        (PROPOSED, OPENED),
        (PROPOSED, OPENED, TRACKING),
    ):
        thesis = make(db)
        if steps:
            thesis = walk(
        repo,
                thesis,
                *steps,
                stop_price=27.0,
                target_price=40.0,
                reward_risk=2.5,
            )
        reached.append(
            repo.advance(USER, thesis.id, INVALIDATED, actor_id=USER, now=T2).status
        )
        repo.advance(USER, thesis.id, ARCHIVED, actor_id=USER, now=T2)
    assert reached == [INVALIDATED] * 4


# --- 用户隔离（同 watchlists 的形态）---------------------------------------------------------


def test_reads_are_isolated_by_user(db):
    """另一个用户既查不到也推不动：隔离在仓库层兜底，不靠调用方自觉（D4）。"""
    repo = ThesisRepo(db)
    thesis = make(db)
    assert repo.get(OTHER, thesis.id) is None
    assert repo.list(OTHER) == []
    with pytest.raises(StorageError):
        repo.advance(OTHER, thesis.id, PROPOSED, actor_id=OTHER, now=T1)


def test_list_carries_every_live_thesis_and_filters_by_status(db):
    repo = ThesisRepo(db)
    first = make(db)
    second = make(db, code="600519")
    repo.advance(USER, first.id, PROPOSED, actor_id=USER, now=T1)
    assert {t.id for t in repo.list(USER)} == {first.id, second.id}
    assert [t.id for t in repo.list(USER, status=PROPOSED)] == [first.id]
    assert [t.id for t in repo.list(USER, status=OBSERVING)] == [second.id]


def test_the_admin_audit_view_is_read_only(db):
    """`list_all` 只给全部用户的一屏，没有任何配套写方法（同 watchlists）。"""
    repo = ThesisRepo(db)
    make(db)
    make(db, user_id=OTHER)
    assert len(repo.list_all()) == 2
    assert not hasattr(repo, "advance_all")


# --- 参数形状：价位、胜率口径、键形态 ---------------------------------------------------------


def test_an_ordered_price_set_is_required(db):
    """买入区上下沿写反了不报错才是问题：屏幕上会印出一个空区间。"""
    with pytest.raises(ValidationError, match="区间"):
        make(db, entry_low=33.0, entry_high=30.0)


@pytest.mark.parametrize("column", ["entry_low", "entry_high", "stop_price", "target_price"])
def test_a_non_positive_price_is_refused(db, column):
    for bad in (0.0, -1.0, float("nan")):
        with pytest.raises(ValidationError):
            make(db, **{column: bad})


def test_a_percent_caliber_win_rate_is_refused(db):
    """`win_rate=55` 是「忘了 ÷100」的形状（D58 那一族）：小数口径下胜率不可能 > 1，
    收下它，§8.3 那条「胜率 < 30 样本不许排序」的闸门就跟着读一个 55 倍的数。"""
    with pytest.raises(ValidationError, match="0.55"):
        make(db, win_rate=55.0)
    thesis = make(db, win_rate=0.55, win_rate_samples=120)
    assert thesis.win_rate == pytest.approx(0.55)


def test_a_negative_sample_count_is_refused(db):
    with pytest.raises(ValidationError):
        make(db, win_rate_samples=-3)


def test_the_key_is_normalized_like_every_other_table(db):
    """`hk` / `700` 这种键一旦落库，后面按 `HK`/`00700` 查行情一律查不到（D46、D50）。"""
    repo = ThesisRepo(db)
    thesis = make(db, market="hk", code="700")
    assert (thesis.market, thesis.code) == ("HK", "00700")
    assert repo.get(USER, thesis.id) is not None
    assert [t.id for t in repo.list(USER)] == [thesis.id]


def test_a_thesis_with_no_actor_is_refused(db):
    """事件行没有 actor 就回答不了「当时是谁改的」，那一列非空是时间线的下限。"""
    with pytest.raises(ValidationError):
        ThesisRepo(db).create(
            user_id=USER, market="A", code="601088", style="value", actor_id="", now=T0
        )
